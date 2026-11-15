#!/usr/bin/env python3
"""Hash lock for the requirements files that hosted CI installs.

Run from the repository root:

    python -X utf8 -B tools/release/hash_lock.py

It finds every requirements file that a workflow under .github/workflows installs, both as
the workflows stand and as the patches in tools/release/proposals/ would leave them, and
fails (exit 1, one line per problem) unless each one:

- has only exact pins (name==version, an optional environment marker) and at least one
  --hash=sha256 on every pin, each hash its own token, with no other pip option;
- holds exactly the hashes recorded for it in tools/release/ci-lock.json, whose entries name
  the file of that very release each hash belongs to;
- pins every dependency of every pin that applies to a job installing it (a dependency that
  requests extras is refused: the check does not follow extras); and
- has, for every job that installs it (its runner and Python version), a wheel of every pin
  that applies there.

Workflows are read exactly as YAML and bash read them, in the forms this repository's
workflows use; every other form is a problem wherever it appears (the comment above
`read_yaml` lists both). Each `run:` value is split into commands and words as bash does it,
and every command that runs an installer program or holds installer text must be a pip
install of requirements files in a job the check found, in a bash step; installer text in
any other value, a step that uses an action the check has not read (ACTIONS), and a command
named by an expansion are each a problem, never "nothing installed". What a command
computes at run time (variables, `eval`, the values of `${{ }}` expressions) is not read.
The gate (tools/ci_gate.py) installs nothing; the check makes
sure it stays that way.
Stdlib only and offline; tools/release/relock.py is the one script that reads PyPI.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RECORD = "tools/release/ci-lock.json"
PROPOSALS = "tools/release/proposals"
WORKFLOWS = ".github/workflows"
GATE = "tools/ci_gate.py"
RECORD_FORMAT = 1

HASH_TOKEN = re.compile(r"--hash=(\S*)")
HASH_OPTION = re.compile(r"--hash=sha256:([0-9a-f]{64})")
NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
RELEASE = r"[0-9]+(?:\.[0-9]+)*"
PIN = re.compile(rf"({NAME})==({RELEASE})(?:\s*;\s*(\S.*))?")
COMMENT = re.compile(r"(^|\s+)#.*$")
# Any line that may install Python packages. It is deliberately wide: a line it matches must
# then be read as one or more pip installs of hash-locked files, or the check fails.
INSTALLER = re.compile(r"(?:-m\s*pip\b|\bpip3?\b|\bpipx\b|\buv\b|\bpoetry\b|\bconda\b|\bmamba\b)[^\n]*\binstall\b"
                       r"|\bpip-sync\b|\buv\s+sync\b|\beasy_install\b")
ALLOWED_PIP_OPTIONS = {"--only-binary=:all:", "--require-hashes", "--no-cache-dir", "-q", "--quiet",
                       "--disable-pip-version-check", "--no-input"}


class LockError(ValueError):
    """A requirements line, marker, version or specifier this check refuses."""


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# --- requirements files -------------------------------------------------------
@dataclass(frozen=True)
class Pin:
    name: str          # canonical
    version: str
    marker: str | None
    hashes: frozenset[str]
    line: int


def logical_lines(text: str) -> list[tuple[int, str]]:
    """Join backslash continuations and drop comments, as pip does.

    A comment inside a continued requirement is refused: pip would cut the requirement at
    the comment and read the rest of it as separate lines.
    """
    out: list[tuple[int, str]] = []
    pending: list[str] = []
    start = 0
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        continued = line.endswith("\\")
        body = line[:-1] if continued else line
        if pending and re.match(r"\s*#", body):
            raise LockError(f"line {number}: a comment inside a continued requirement")
        if continued and COMMENT.search(body):
            raise LockError(f"line {number}: a comment on a continued line")
        if not pending:
            start = number
        pending.append(COMMENT.sub("", body).strip())
        if continued:
            continue
        joined = " ".join(part for part in pending if part)
        pending = []
        if joined:
            out.append((start, joined))
    if pending:
        raise LockError(f"line {start}: the file ends inside a continued line")
    return out


def parse_requirements(text: str) -> list[Pin]:
    """Strict: exact pins with sha256 hashes and nothing else (no -r, -c, -e, index or URL)."""
    pins: list[Pin] = []
    seen: dict[str, int] = {}
    for number, line in logical_lines(text):
        # Token by token, as pip reads it: the requirement first, then each --hash as a token of
        # its own. A --hash glued to the requirement is part of the requirement to pip.
        tokens = line.split()
        first_hash = next((i for i, token in enumerate(tokens) if token.startswith("--hash")), len(tokens))
        hashes: set[str] = set()
        for token in tokens[first_hash:]:
            match = HASH_OPTION.fullmatch(token)
            if not match:
                raise LockError(f"line {number}: unsupported hash {token!r} (each --hash=sha256:<64 hex digits> "
                                "is its own token, after the requirement)")
            hashes.add(match.group(1))
        rest = " ".join(tokens[:first_hash])
        if "--hash" in rest:
            raise LockError(f"line {number}: a --hash must be its own token, separated by whitespace: {rest!r}")
        if re.search(r"(^|\s)-", rest):
            raise LockError(f"line {number}: pip option not allowed in a locked file: {rest!r}")
        match = PIN.fullmatch(rest)
        if not match:
            raise LockError(f"line {number}: not an exact pin (name==version): {rest!r}")
        if not hashes:
            raise LockError(f"line {number}: {match.group(1)}=={match.group(2)} has no --hash")
        name = canonical(match.group(1))
        if name in seen:
            raise LockError(f"line {number}: {name} is pinned twice (first on line {seen[name]})")
        seen[name] = number
        marker = match.group(3).strip() if match.group(3) else None
        if marker is not None:
            parse_marker(marker)  # refuse what the evaluator cannot read, now
        pins.append(Pin(name, match.group(2), marker, frozenset(hashes), number))
    if not pins:
        raise LockError("no pins")
    return pins


# --- versions and specifiers (numeric releases only; anything else is refused) --
def parse_version(text: str) -> tuple[int, ...]:
    text = text.strip()
    if not re.fullmatch(RELEASE, text):
        raise LockError(f"version {text!r} is not a plain release (digits and dots)")
    return tuple(int(part) for part in text.split("."))


def _padded(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)), b + (0,) * (width - len(b))


def _compare(version: tuple[int, ...], op: str, text: str) -> bool:
    if op in ("==", "!=") and text.endswith(".*"):
        prefix = parse_version(text[:-2])
        padded = version + (0,) * max(0, len(prefix) - len(version))
        hit = padded[:len(prefix)] == prefix
        return hit if op == "==" else not hit
    if op == "~=":
        floor = parse_version(text)
        if len(floor) < 2:
            raise LockError(f"~={text} needs at least two release parts")
        return _compare(version, ">=", text) and version[:len(floor) - 1] == floor[:-1]
    left, right = _padded(version, parse_version(text))
    return {"==": left == right, "!=": left != right, ">=": left >= right, "<=": left <= right,
            ">": left > right, "<": left < right}[op]


SPEC = re.compile(r"\s*(~=|==|!=|>=|<=|>|<)\s*([0-9][0-9.]*(?:\.\*)?)\s*")


def satisfies(version: str, specifier: str) -> bool:
    """True if version meets every clause of a comma-separated specifier ("" is any version)."""
    parsed = parse_version(version)
    for clause in filter(None, (part.strip() for part in specifier.split(","))):
        match = SPEC.fullmatch(clause)
        if not match:
            raise LockError(f"specifier clause {clause!r} is not supported")
        if not _compare(parsed, match.group(1), match.group(2)):
            return False
    return True


# --- environment markers ------------------------------------------------------
MARKER_TOKEN = re.compile(r"\s*(?:(\()|(\))|(and|or)\b|(===|==|!=|>=|<=|~=|>|<|not\s+in\b|in\b)|"
                          r"'([^']*)'|\"([^\"]*)\"|([a-z_]+)|(\S))")
MARKER_VARIABLES = {"python_version", "python_full_version", "implementation_name",
                    "platform_python_implementation", "sys_platform", "platform_system",
                    "platform_machine", "os_name", "extra"}
VERSION_VARIABLES = {"python_version", "python_full_version"}


def _marker_tokens(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        if not text[pos:].strip():
            break
        match = MARKER_TOKEN.match(text, pos)
        if not match or match.group(8):
            raise LockError(f"marker {text!r}: cannot read at {text[pos:]!r}")
        pos = match.end()
        lpar, rpar, boolean, op, single, double, word, _ = match.groups()
        if lpar:
            tokens.append(("(", "("))
        elif rpar:
            tokens.append((")", ")"))
        elif boolean:
            tokens.append(("bool", boolean))
        elif op:
            if op in ("===", "in") or op.startswith("not"):
                raise LockError(f"marker {text!r}: operator {op!r} is not supported")
            tokens.append(("op", op))
        elif single is not None or double is not None:
            tokens.append(("str", single if single is not None else double))
        else:
            if word not in MARKER_VARIABLES:
                raise LockError(f"marker {text!r}: variable {word!r} is not supported")
            tokens.append(("var", word))
    return tokens


def parse_marker(text: str):
    """Parse into nested tuples: ("and"|"or", left, right) or ("cmp", var, op, value)."""
    tokens = _marker_tokens(text)
    pos = 0

    def peek() -> tuple[str, str] | None:
        return tokens[pos] if pos < len(tokens) else None

    def take(kind: str) -> str:
        nonlocal pos
        token = peek()
        if token is None or token[0] != kind:
            raise LockError(f"marker {text!r}: expected {kind}")
        pos += 1
        return token[1]

    def atom():
        nonlocal pos
        token = peek()
        if token and token[0] == "(":
            pos += 1
            node = expression()
            take(")")
            return node
        if token and token[0] == "var":
            var = take("var")
            op = take("op")
            return ("cmp", var, op, take("str"))
        if token and token[0] == "str":
            value = take("str")
            op = take("op")
            flipped = {">": "<", "<": ">", ">=": "<=", "<=": ">="}.get(op, op)
            if op == "~=":
                raise LockError(f"marker {text!r}: ~= with the value first")
            return ("cmp", take("var"), flipped, value)
        raise LockError(f"marker {text!r}: expected a comparison")

    def conjunction():
        nonlocal pos
        node = atom()
        while peek() == ("bool", "and"):
            pos += 1
            node = ("and", node, atom())
        return node

    def expression():
        nonlocal pos
        node = conjunction()
        while peek() == ("bool", "or"):
            pos += 1
            node = ("or", node, conjunction())
        return node

    tree = expression()
    if pos != len(tokens):
        raise LockError(f"marker {text!r}: unexpected {tokens[pos][1]!r}")
    return tree


def marker_applies(marker: str | None, env: dict[str, str]) -> bool:
    if marker is None:
        return True

    def evaluate(node) -> bool:
        if node[0] == "and":
            return evaluate(node[1]) and evaluate(node[2])
        if node[0] == "or":
            return evaluate(node[1]) or evaluate(node[2])
        _, var, op, value = node
        actual = env.get(var, "")
        if var in VERSION_VARIABLES:
            return _compare(parse_version(actual), op, value)
        if op not in ("==", "!="):
            raise LockError(f"marker: {var} {op} {value!r} compares a string by order")
        return (actual == value) == (op == "==")

    return evaluate(parse_marker(marker))


# --- dependencies as PyPI's metadata states them -------------------------------
REQUIREMENT = re.compile(rf"\s*({NAME})\s*(\[[^\]]*\])?\s*(?:\(([^)]*)\)|([^;]*))\s*(?:;\s*(.*))?")


def dependency_marker(text: str) -> str | None:
    """The environment marker of a Requires-Dist entry, read before the rest of it."""
    return text.partition(";")[2].strip() or None


def parse_dependency(text: str) -> tuple[str, str, str | None]:
    """(name, specifier, marker). Refuses a dependency that requests extras: this check does not
    follow an extra's own dependencies, so it must not pretend the plain package is enough."""
    match = REQUIREMENT.fullmatch(text)
    if not match:
        raise LockError(f"dependency {text!r} cannot be read")
    if match.group(2) and match.group(2).strip("[] "):
        raise LockError(f"dependency {text!r} requests extras {match.group(2)}; this check does not follow extras")
    spec = (match.group(3) if match.group(3) is not None else match.group(4) or "").strip()
    return canonical(match.group(1)), spec, (match.group(5) or "").strip() or None


def dist_release(filename: str) -> tuple[str, str] | None:
    """(canonical name, version) a wheel or sdist file name states, or None if it states none."""
    if filename.endswith(".whl"):
        parts = filename[:-4].split("-")
        return (canonical(parts[0]), parts[1]) if len(parts) in (5, 6) else None
    match = re.fullmatch(r"(.+)-([0-9][^-]*)\.(?:tar\.gz|zip)", filename)
    return (canonical(match.group(1)), match.group(2)) if match else None


# --- CI targets and wheel tags ---------------------------------------------------
# runs-on label -> (system, machine, glibc or macOS floor). A label not listed here fails the
# check, so a new runner is added on purpose. The floors are low on purpose: a newer image
# only accepts more wheels, never fewer.
RUNNERS = {
    "ubuntu-latest": ("linux", "x86_64", (2, 39)),
    "ubuntu-24.04": ("linux", "x86_64", (2, 39)),
    "ubuntu-22.04": ("linux", "x86_64", (2, 35)),
    "windows-2025": ("windows", "AMD64", None),
    "windows-latest": ("windows", "AMD64", None),
    "macos-latest": ("macos", "arm64", (14, 0)),
    "macos-14": ("macos", "arm64", (14, 0)),
    "macos-15": ("macos", "arm64", (15, 0)),
}
PYTHON = re.compile(r"3\.(\d+)(t?)")
LEGACY_MANYLINUX = {"manylinux1": (2, 5), "manylinux2010": (2, 12), "manylinux2014": (2, 17)}


@dataclass(frozen=True)
class Target:
    runner: str
    python: str  # "3.12", or "3.14t" for the free-threaded build

    @property
    def minor(self) -> int:
        return int(self.python_match().group(1))

    @property
    def threaded(self) -> bool:
        return self.python_match().group(2) == "t"

    def python_match(self) -> re.Match:
        match = PYTHON.fullmatch(self.python)
        if not match:
            raise LockError(f"python-version {self.python!r} is not 3.N or 3.Nt")
        return match

    def env(self) -> dict[str, str]:
        system, machine, _ = RUNNERS[self.runner]
        return {
            "python_version": f"3.{self.minor}",
            "python_full_version": f"3.{self.minor}.0",
            "implementation_name": "cpython",
            "platform_python_implementation": "CPython",
            "sys_platform": {"linux": "linux", "windows": "win32", "macos": "darwin"}[system],
            "platform_system": {"linux": "Linux", "windows": "Windows", "macos": "Darwin"}[system],
            "platform_machine": machine,
            "os_name": "nt" if system == "windows" else "posix",
            "extra": "",
        }

    def __str__(self) -> str:
        return f"{self.runner}/python {self.python}"


def _platform_ok(tag: str, target: Target) -> bool:
    if tag == "any":
        return True
    system, _, floor = RUNNERS[target.runner]
    if system == "windows":
        return tag == "win_amd64"
    if system == "linux":
        match = re.fullmatch(r"manylinux_(\d+)_(\d+)_x86_64", tag)
        if match:
            return (int(match.group(1)), int(match.group(2))) <= floor
        legacy = re.fullmatch(r"(manylinux1|manylinux2010|manylinux2014)_x86_64", tag)
        return bool(legacy) and LEGACY_MANYLINUX[legacy.group(1)] <= floor
    match = re.fullmatch(r"macosx_(\d+)_(\d+)_(arm64|universal2)", tag)
    if not match:
        return False
    version, binary = (int(match.group(1)), int(match.group(2))), match.group(3)
    if version[0] >= 11:  # one tag per yearly release from 11 on, minor always 0
        return version[1] == 0 and version <= floor
    return version[0] == 10 and 4 <= version[1] <= 16 and binary == "universal2"


def _python_abi_ok(python: str, abi: str, target: Target) -> bool:
    minor, threaded = target.minor, target.threaded
    cp = re.fullmatch(r"cp3(\d+)", python)
    py = re.fullmatch(r"py3(\d*)", python)
    if py:
        return abi == "none" and (not py.group(1) or int(py.group(1)) <= minor)
    if not cp:
        return False
    built_for = int(cp.group(1))
    if abi == f"cp3{minor}{'t' if threaded else ''}" or (abi == "none" and not threaded):
        return built_for == minor
    if abi == "abi3":  # the stable ABI does not reach the free-threaded build
        return not threaded and built_for <= minor
    if abi == "abi3t":
        return threaded and built_for <= minor
    return False


def wheel_installs(filename: str, target: Target) -> bool:
    if not filename.endswith(".whl"):
        return False
    parts = filename[:-4].split("-")
    if len(parts) not in (5, 6):
        return False
    pythons, abis, platforms = parts[-3].split("."), parts[-2].split("."), parts[-1].split(".")
    return any(_python_abi_ok(p, a, target) for p in pythons for a in abis) and any(
        _platform_ok(tag, target) for tag in platforms)


# --- what the workflows install ----------------------------------------------------
@dataclass(frozen=True)
class Install:
    source: str     # workflow file name, with " (proposed)" when a proposal's result
    job: str
    file: str       # repository-relative requirements path
    target: Target


# --- workflows: read exactly as YAML and bash read them, or refused -------------------
# What this check does not decode it cannot vouch for, so every form it does not read is a
# problem wherever it appears, whether or not the text looks like an install.
#
# YAML: block mappings and lists; one-line plain, single-quoted and double-quoted values
# (a double-quoted value with no backslash); `|` and `>` blocks with an optional chomping
# indicator; one-line flow lists of plain items. Refused: anchors, aliases, tags, merge keys,
# flow mappings, a value that continues on the next line, backslash escapes, an explicit
# block indentation indicator, a tab in indentation, document markers, directives, complex
# keys, a key twice in one mapping, control characters, and line breaks other than LF and CRLF.
#
# Bash: every `run:` value is split into simple commands and words as bash does it (quotes,
# backslashes, `$'...'`, comments, line continuations, here-documents, redirections,
# `$(...)` and backticks, whose commands are read too), after GitHub's `${{ }}` expressions,
# whose values are known only at run time. A command that runs an installer program, or
# whose words hold installer text, must be a pip install of hash-locked files, in a job this
# check found, in a bash step. The scripts a command gives to `eval` or to a shell (`-c`, a
# here-document or here-string) are read the same way, the command found at its command
# position: after assignments, reserved words and the wrappers in WRAPPERS (`command -v` looks a
# command up and runs nothing). A `$NAME` expansion is tried with every literal value the script or the job's
# `env:` gives it; an expansion it cannot resolve before an installer verb, a script for
# `eval` or a shell built at run time, a shell reading its script from a pipe, and `source`
# or `.` of any file are problems. What the shell computes at run time beyond that (text
# assembled from pieces, files written and then run) is not read.

YAML_BREAKS = re.compile("[" + "".join(chr(c) for c in (0x0D, 0x85, 0x2028, 0x2029)) + "]")
CONTROL = re.compile("[" + "".join(chr(c) for c in (*range(0x00, 0x09), 0x0B, 0x0C, *range(0x0E, 0x20), 0x7F)) + "]")
PLAIN_KEY = r"[^\s#'\"&*!?|>%@`\[\]{},:-][^:#]*?"
KEY = re.compile(r"(" + PLAIN_KEY + r"|\"[^\"\\]*\"|'(?:[^']|'')*')[ \t]*:(?:[ \t]+(.*))?$")
BLOCK_HEADER = re.compile(r"[|>]([+-]?)(?:[ \t]+#.*)?")
FLOW_ITEM = r"[^\s,\[\]{}#&*!|>'\"%@`?:\\-][^,\[\]{}#'\"\\:]*?"
FLOW_LIST = re.compile(rf"(\[[ \t]*(?:{FLOW_ITEM}(?:[ \t]*,[ \t]*{FLOW_ITEM})*[ \t]*,?)?[ \t]*\])(?:[ \t]+#.*)?")
ENV_KEY = re.compile(r"[A-Z_][A-Z0-9_]*")
# Actions a step may use. Each one installs no Python package from outside the lock; a new
# action is added here only after reading what it installs. A reusable workflow of this
# repository (`./.github/workflows/<file>`) is read as a workflow of its own.
ACTIONS = {"actions/checkout", "actions/setup-python", "actions/upload-artifact",
           "actions/download-artifact",
           "anthropics/claude-code-action"}  # used only by the workflows retire-review-workflows.patch removes
INSTALLER_PROGRAM = re.compile(r"(?:.*/)?(?:pip3?(?:\.\d+)?|pipx|uvx?|poetry|conda|mamba|micromamba|pip-sync"
                               r"|easy_install)(?:\.exe)?")
INSTALLER_VERBS = {"install", "sync"}


class Unread(Exception):
    """A YAML form this check does not read."""


@dataclass(frozen=True)
class Scalar:
    line: int     # 0-based line of its key or list entry
    path: tuple   # keys (str) and list positions (int) from the top of the file
    text: str     # the value as YAML decodes it


class _Lines(list):
    """A file's lines, and whether its last line ends with a line break."""
    final_break = True


class _Frame:
    def __init__(self, kind: str, indent: int, path: tuple) -> None:
        self.kind, self.indent, self.path = kind, indent, path
        self.keys: set[str] = set()
        self.count = 0
        self.pending = None  # the key or list position whose value starts on the next line


def read_yaml(text: str) -> tuple[list[Scalar], list[tuple[int, str]]]:
    """Every scalar of a workflow with its path, and (line, why) for each form not read."""
    lines = _Lines(text.replace("\r\n", "\n").split("\n"))
    if lines[-1] == "":
        lines.pop()  # the final line break ends the last line; it starts none
    else:
        lines.final_break = False
    scalars: list[Scalar] = []
    problems: list[tuple[int, str]] = []
    frames: list[_Frame] = []
    index, skip_deeper_than = 0, None
    while index < len(lines):
        line = lines[index]
        bare = line.lstrip(" ")
        indent = len(line) - len(bare)
        blank = not bare.strip(" \t") or bare.lstrip(" \t").startswith("#")
        if skip_deeper_than is not None and (blank or indent > skip_deeper_than):
            index += 1
            continue
        skip_deeper_than = None
        try:
            if YAML_BREAKS.search(line):
                raise Unread("a line break other than LF, which this check does not read")
            if CONTROL.search(line):
                raise Unread("a control character, which YAML does not allow")
            if blank:
                index += 1
                continue
            if bare.startswith("\t"):
                raise Unread("a tab in the indentation")
            index = _yaml_line(lines, index, indent, bare, frames, scalars)
        except Unread as exc:
            problems.append((index, str(exc)))
            skip_deeper_than = indent
            index += 1
    return scalars, problems


def _yaml_line(lines: list[str], index: int, indent: int, bare: str, frames: list[_Frame],
               scalars: list[Scalar]) -> int:
    while frames and frames[-1].indent > indent:
        frames.pop()
    top = frames[-1] if frames else None
    dash = bare == "-" or bare.startswith("- ")
    if top is not None and top.pending is not None and (
            indent > top.indent or (indent == top.indent and top.kind == "map" and dash)):
        frame = _Frame("seq" if dash else "map", indent, top.path + (top.pending,))
        top.pending = None
        frames.append(frame)
    elif top is not None and indent == top.indent:
        if top.kind == "seq" and not dash:
            frames.pop()  # the list ends; the line belongs to the mapping that holds it
            return _yaml_line(lines, index, indent, bare, frames, scalars)
        if top.kind == "map" and dash:
            raise Unread("a list entry where its mapping continues")
        top.pending = None  # a key or entry left empty above holds null
        frame = top
    elif top is None:
        frame = _Frame("seq" if dash else "map", indent, ())
        frames.append(frame)
    else:
        raise Unread("a line deeper than the value above it (a value that continues on the next line, "
                     "or an indentation this check cannot follow)")
    return _yaml_content(lines, index, indent, bare, frame, frames, scalars)


def _yaml_content(lines: list[str], index: int, column: int, text: str, frame: _Frame,
                  frames: list[_Frame], scalars: list[Scalar]) -> int:
    if frame.kind == "seq":
        position = frame.count
        frame.count += 1
        after = text[1:]
        rest = after.lstrip(" ")
        inner = column + 1 + len(after) - len(rest)
        path = frame.path + (position,)
        if not rest or rest.startswith("#"):
            frame.pending = position
            return index + 1
        if rest == "-" or rest.startswith("- ") or KEY.match(rest):
            child = _Frame("seq" if rest[0] == "-" else "map", inner, path)
            frames.append(child)
            return _yaml_content(lines, index, inner, rest, child, frames, scalars)
        return _yaml_value(rest, lines, index, frame.indent, path, scalars)[0]
    key = KEY.match(text)
    if not key:
        raise Unread("a line that is neither `key: value` nor a list entry")
    raw = key.group(1)
    name = raw[1:-1] if raw[0] == '"' else raw[1:-1].replace("''", "'") if raw[0] == "'" else raw.strip()
    if name == "<<":
        raise Unread("a merge key, which this check does not read")
    if name in frame.keys:
        raise Unread(f"the key {name!r} twice in one mapping")
    frame.keys.add(name)
    after, pending = _yaml_value(key.group(2) or "", lines, index, frame.indent, frame.path + (name,), scalars)
    if pending:
        frame.pending = name
    return after


def _yaml_value(text: str, lines: list[str], index: int, parent: int, path: tuple,
                scalars: list[Scalar]) -> tuple[int, bool]:
    """Read the value written after a key or a list dash. Returns the next line to read and
    whether the value is a mapping or list on the lines that follow."""
    value = text.strip(" \t")
    if not value or value.startswith("#"):
        return index + 1, True
    first = value[0]
    if first in "&*!":
        raise Unread("an anchor, alias or tag, which this check does not read")
    if first == "{":
        raise Unread("a flow mapping, which this check does not read")
    if first in "?%@`,]}" or value == "-" or value.startswith("- "):
        raise Unread(f"a value starting with {value[:2] if first == '-' else first!r}, which this check does not read")
    if first in "|>":
        header = BLOCK_HEADER.fullmatch(value)
        if not header:
            raise Unread("an explicit block indentation indicator, which this check does not read"
                         if re.match(r"[|>][+-]?[0-9]", value) else "a block header this check cannot read")
        body, after = _block_lines(lines, index + 1, parent)
        unbroken = after == len(lines) and not getattr(lines, "final_break", True) and bool(body)
        scalars.append(Scalar(index, path, _block_text(first, header.group(1), body, unbroken)))
        return after, False
    if first == "[":
        flow = FLOW_LIST.fullmatch(value)
        if not flow:
            raise Unread("a flow list this check cannot read (it reads one line of plain items)")
        decoded = flow.group(1)
        rest = ""
    elif first == "'":
        quoted = re.match(r"'((?:[^']|'')*)'", value)
        if not quoted:
            raise Unread("a quoted value that continues on the next line, which this check does not read")
        decoded, rest = quoted.group(1).replace("''", "'"), value[quoted.end():]
    elif first == '"':
        quoted = re.match(r'"([^"\\]*)"', value)
        if not quoted:
            raise Unread("a backslash escape in a double-quoted value, which this check does not read"
                         if re.match(r'"[^"\\]*\\', value) else
                         "a quoted value that continues on the next line, which this check does not read")
        decoded, rest = quoted.group(1), value[quoted.end():]
    else:
        decoded = re.split(r"[ \t]+#", value, maxsplit=1)[0].rstrip(" \t")
        rest = ""
        if re.search(r":(?:[ \t]|$)", decoded):
            raise Unread("a `: ` inside a plain value, which this check does not read")
    if rest.strip(" \t") and not re.match(r"[ \t]+#", rest):
        raise Unread("text after a quoted value")
    scalars.append(Scalar(index, path, decoded))
    return index + 1, False


def _block_lines(lines: list[str], start: int, parent: int) -> tuple[list[str], int]:
    """The lines of a `|` or `>` block with its indentation removed, and the next line to read.
    A block holds the lines deeper than its parent; its indentation is its first line's."""
    end = start
    while end < len(lines) and (not lines[end].strip(" ") or len(lines[end]) - len(lines[end].lstrip(" ")) > parent):
        end += 1
    raw = lines[start:end]
    filled = [line for line in raw if line.strip(" ")]
    indent = len(filled[0]) - len(filled[0].lstrip(" ")) if filled else 0
    body = []
    for line in raw:
        if not line.strip(" "):
            if len(line) > indent:
                raise Unread("a blank line of a block value with more spaces than the block's indentation")
            body.append("")
        elif line.strip(" \t") == "":
            raise Unread("a line of only tabs and spaces in a block value")
        elif len(line) - len(line.lstrip(" ")) < indent:
            raise Unread("a line of a block value less indented than its first line")
        else:
            body.append(line[indent:])
    return body, end


def _block_text(style: str, chomping: str, body: list[str], unbroken: bool = False) -> str:
    """YAML's reading of a block: `|` keeps every line break; `>` folds the break between two
    lines that start with no space into a space, keeps it next to a more-indented line, and
    turns each empty line into a line break. The chomping indicator decides the final breaks
    (`unbroken`: the block's last line ends the file with no line break)."""
    end = len(body)
    while end and body[end - 1] == "":
        end -= 1
    content = body[:end]
    breaks = len(body) - end + 1 - unbroken  # the line breaks after the last line with text
    if not content:
        return "\n" * (len(body) - unbroken) if chomping == "+" else ""
    if style == "|":
        text = "\n".join(content)
    else:
        parts: list[str] = []
        previous = None
        empty = 0
        for line in content:
            if line == "":
                empty += 1
                continue
            if previous is None:
                parts.append("\n" * empty)
            elif previous[:1] not in (" ", "\t") and line[:1] not in (" ", "\t"):
                parts.append(" " if empty == 0 else "\n" * empty)
            else:
                parts.append("\n" * (empty + 1))
            parts.append(line)
            previous, empty = line, 0
        text = "".join(parts)
    return text + {"-": "", "": "\n" * min(breaks, 1), "+": "\n" * breaks}[chomping]


@dataclass(frozen=True)
class Word:
    text: str
    dynamic: bool  # it holds an expansion or a substitution, whose value is known only at run time
    stdin: bool = False  # text for the command's standard input: a here-document or here-string


SHELL_OPERATORS_BY_LENGTH = (";;&", "&>>", "<<-", "<<<", "&&", "||", ";;", ";&", "|&", ">>", "<<",
                             "<>", ">|", ">&", "<&", "&>", ";", "&", "|", "(", ")", "<", ">", "\n")
REDIRECTIONS = {"&>>", "<<-", "<<<", ">>", "<<", "<>", ">|", ">&", "<&", "&>", "<", ">"}
ANSI_C = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
          "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}
GLOB_OR_BRACE = re.compile(r"[*?]|\[[^\]]+\]|\{[^{}]*(?:,|\.\.)[^{}]*\}")
GITHUB_EXPRESSION = re.compile(r"\$\{\{.*?\}\}", re.S)
RESERVED_WORDS = {"!", "{", "}", "if", "then", "else", "elif", "fi", "do", "done", "while", "until", "for",
                  "in", "case", "esac", "select", "function", "coproc", "[[", "]]"}


class _Bash:
    """Bash's split of a script into simple commands and words (quote removal done), with the
    commands of every command substitution among them."""

    def __init__(self, text: str, expressions: list[str], commands: list[list[Word]]) -> None:
        self.s, self.i, self.expressions, self.commands = text, 0, expressions, commands
        self.heredocs: list[tuple[str, bool, bool, list[Word]]] = []

    def expression(self, word: list[str]) -> None:
        end = self.s.index("\x01", self.i)
        word.append(self.expressions[int(self.s[self.i + 1:end])])
        self.i = end + 1

    def script(self, stop: str | None) -> None:
        s = self.s
        command: list[Word] = []
        self.commands.append(command)
        word: list[str] = []
        plain: list[str] = []  # the word's unquoted characters, for pathname and brace expansion
        dynamic = started = drop = herestring = False
        depth = 0

        def end_word() -> None:
            nonlocal word, plain, dynamic, started, drop, herestring
            if GLOB_OR_BRACE.search("".join(plain)):
                dynamic = True
            if started and not drop:
                command.append(Word("".join(word), dynamic, herestring))
            if started:
                drop = herestring = False
            word, plain, dynamic, started = [], [], False, False

        def new_command() -> None:
            nonlocal command, drop, herestring
            end_word()
            drop = herestring = False
            command = []
            self.commands.append(command)

        while self.i < len(s):
            c = s[self.i]
            if c == "\\":
                following = s[self.i + 1:self.i + 2]
                if following != "\n":
                    word.append(following or "\\")
                    started = True
                self.i += 2
            elif c == "'":
                end = s.find("'", self.i + 1)
                if end < 0:
                    raise LockError("an unterminated single quote")
                piece = s[self.i + 1:end]
                dynamic |= "\x00" in piece
                word.append(re.sub(r"\x00(\d+)\x01", lambda m: self.expressions[int(m.group(1))], piece))
                started, self.i = True, end + 1
            elif c == '"':
                self.i += 1
                started = True
                dynamic |= self.double(word)
            elif c == "\x00":
                self.expression(word)
                started = dynamic = True
            elif c == "$":
                started = True
                dynamic |= self.dollar(word, quoted=False)
            elif c == "`":
                self.backtick(word)
                started = dynamic = True
            elif c in "<>" and s[self.i + 1:self.i + 2] == "(" and not started:
                start = self.i  # process substitution: a word (a /dev/fd path) whose commands run
                self.i += 2
                self.script(stop=")")
                word.append(s[start:self.i])
                started = dynamic = True
            elif c == "#" and not started:
                end = s.find("\n", self.i)
                self.i = len(s) if end < 0 else end
            elif c in " \t":
                end_word()
                self.i += 1
            else:
                op = next((o for o in SHELL_OPERATORS_BY_LENGTH if s.startswith(o, self.i)), None)
                if op is None:
                    word.append(c)
                    plain.append(c)
                    started = True
                    self.i += 1
                    continue
                if op in REDIRECTIONS and started and not dynamic and "".join(word).isdigit():
                    word, started = [], False  # a file descriptor number: `2>`
                end_word()
                self.i += len(op)
                if op == ")" and stop == ")" and depth == 0:
                    return
                if op in REDIRECTIONS:
                    if op in ("<<", "<<-"):
                        self.heredoc(op == "<<-", command)
                    elif op == "<<<":
                        herestring = True  # the next word is text for the command's standard input
                    else:
                        drop = True  # the target of a redirection is no word of the command
                    continue
                depth += {"(": 1, ")": -1}.get(op, 0)
                new_command()
                if op == "\n":
                    self.read_heredocs()
        end_word()
        if stop is not None:
            raise LockError("an unterminated $(...)")
        if self.heredocs:
            raise LockError(f"an unterminated here-document ({self.heredocs[0][0]})")

    def double(self, word: list[str]) -> bool:
        s, dynamic = self.s, False
        while self.i < len(s):
            c = s[self.i]
            if c == '"':
                self.i += 1
                return dynamic
            if c == "\\" and s[self.i + 1:self.i + 2] in ('"', "\\", "$", "`", "\n"):
                if s[self.i + 1] != "\n":
                    word.append(s[self.i + 1])
                self.i += 2
            elif c == "$":
                dynamic |= self.dollar(word, quoted=True)
            elif c == "`":
                self.backtick(word)
                dynamic = True
            elif c == "\x00":
                self.expression(word)
                dynamic = True
            else:
                word.append(c)
                self.i += 1
        raise LockError("an unterminated double quote")

    def dollar(self, word: list[str], quoted: bool) -> bool:
        """Read the expansion at `$`; True when its value is known only at run time."""
        s, start = self.s, self.i
        following = s[start + 1:start + 2]
        if s.startswith("$((", start):
            depth, i = 0, start + 1
            while i < len(s):
                depth += {"(": 1, ")": -1}.get(s[i], 0)
                if depth == 0:
                    break
                i += 1
            text = s[start:i + 1]
            if "$(" in text[3:] or "`" in text or "\x00" in text:
                raise LockError("a substitution inside $((...)), which this check does not read")
            word.append(text)
            self.i = i + 1
            return True
        if following == "(":
            self.i = start + 2
            self.script(stop=")")
            word.append(s[start:self.i])
            return True
        if following == "{":
            self.i = start + 2
            scratch: list[str] = []
            while self.i < len(s) and s[self.i] != "}":
                c = s[self.i]
                if c == "\\":
                    self.i += 2
                elif c == "'" and not quoted:
                    end = s.find("'", self.i + 1)
                    if end < 0:
                        raise LockError("an unterminated single quote")
                    self.i = end + 1
                elif c == '"':
                    self.i += 1
                    self.double(scratch)
                elif c == "$":
                    self.dollar(scratch, quoted)
                elif c == "`":
                    self.backtick(scratch)
                elif c == "\x00":
                    self.expression(scratch)
                else:
                    self.i += 1
            if self.i >= len(s):
                raise LockError("an unterminated ${...}")
            self.i += 1
            word.append(s[start:self.i])
            return True
        if following == "'" and not quoted:
            self.i = start + 2
            word.append(self.ansi_c())
            return False
        if following == '"' and not quoted:
            self.i = start + 2
            return self.double(word)
        name = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9@*#?$!-]").match(s, start + 1)
        if name:
            self.i = name.end()
            word.append(s[start:self.i])
            return True
        word.append("$")
        self.i = start + 1
        return False

    def ansi_c(self) -> str:
        """Bash's `$'...'`: the closing quote is found first (a backslash escapes any character),
        then the escapes are decoded as bash decodes them. A NUL byte is refused: bash cuts the
        word there."""
        s, end = self.s, self.i
        while end < len(s) and s[end] != "'":
            end += 2 if s[end] == "\\" else 1
        if end >= len(s):
            raise LockError("an unterminated $'...'")
        content, self.i = s[self.i:end], end + 1
        if "\x00" in content:
            raise LockError("a ${{ }} expression inside $'...', which this check does not read")
        out, i = [], 0
        while i < len(content):
            c, e = content[i], content[i + 1:i + 2]
            if c != "\\":
                out.append(c)
                i += 1
            elif e in ANSI_C:
                out.append(ANSI_C[e])
                i += 2
            elif m := re.compile(r"[0-7]{1,3}").match(content, i + 1):
                out.append(chr(int(m.group(0), 8) & 0xFF))  # an eight-bit character: 0o560 is "p"
                i = m.end()
            elif m := re.compile(r"x([0-9A-Fa-f]{1,2})").match(content, i + 1):
                out.append(chr(int(m.group(1), 16)))
                i = m.end()
            elif m := re.compile(r"u([0-9A-Fa-f]{1,4})|U([0-9A-Fa-f]{1,8})").match(content, i + 1):
                value = int(m.group(1) or m.group(2), 16)
                if value > 0x10FFFF or 0xD800 <= value <= 0xDFFF:
                    raise LockError(f"a $'...' escape for U+{value:X}, which is no character")
                out.append(chr(value))
                i = m.end()
            elif e == "c" and i + 2 < len(content):
                x = content[i + 2]
                out.append(chr(0x7F if x == "?" else ord(x.upper()) & 0x1F))
                i += 4 if x == "\\" and content[i + 3:i + 4] == "\\" else 3
            else:
                out.append("\\" + e)
                i += 2
        text = "".join(out)
        if "\x00" in text:
            raise LockError("a $'...' escape that makes a NUL byte (bash cuts the word there)")
        return text

    def backtick(self, word: list[str]) -> None:
        s, i, inner = self.s, self.i + 1, []
        while i < len(s) and s[i] != "`":
            if s[i] == "\\" and s[i + 1:i + 2] in ("`", "\\", "$"):
                inner.append(s[i + 1])
                i += 2
            else:
                inner.append(s[i])
                i += 1
        if i >= len(s):
            raise LockError("an unterminated backtick")
        sub = _Bash("".join(inner), self.expressions, self.commands)
        sub.script(stop=None)
        word.append(s[self.i:i + 1])
        self.i = i + 1

    def heredoc(self, strip_tabs: bool, command: list[Word]) -> None:
        m = re.compile(r"[ \t]*(?:'([^'\n]*)'|\"([^\"\\$`\n]*)\"|(\\?)([A-Za-z0-9_.+-]+))").match(self.s, self.i)
        if not m:
            raise LockError("a here-document delimiter this check cannot read")
        if self.s[m.end():m.end() + 1] not in ("", " ", "\t", "\n", ";", "&", "|", "<", ">", "(", ")"):
            raise LockError("a here-document delimiter this check cannot read")
        quoted = m.group(4) is None or bool(m.group(3))
        delimiter = m.group(1) if m.group(1) is not None else m.group(2) if m.group(2) is not None else m.group(4)
        self.heredocs.append((delimiter, strip_tabs, quoted, command))
        self.i = m.end()

    def read_heredocs(self) -> None:
        for delimiter, strip_tabs, quoted, command in self.heredocs:
            body = []
            while True:
                if self.i >= len(self.s):
                    raise LockError(f"an unterminated here-document ({delimiter})")
                end = self.s.find("\n", self.i)
                end = len(self.s) if end < 0 else end
                line = self.s[self.i:end]
                self.i = min(end + 1, len(self.s))
                if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                    break
                body.append(line)
            text = "\n".join(body)
            if not quoted:
                if re.search(r"\$\(|`", text):
                    raise LockError("a command substitution in an unquoted here-document, which this check does not read")
                # bash drops a backslash before $, `, \ and a line break (joining the two lines)
                text = re.sub(r"\\([$`\\\n])", lambda m: "" if m.group(1) == "\n" else m.group(1), text)
            command.append(Word(re.sub(r"\x00(\d+)\x01", lambda m: self.expressions[int(m.group(1))], text),
                                "\x00" in text or (not quoted and "$" in text), True))
        self.heredocs = []


def shell_commands(script: str) -> list[list[Word]]:
    """The simple commands of a bash `run:` value, each as its words."""
    if CONTROL.search(script):
        raise LockError("a control character in the script")
    expressions: list[str] = []

    def hold(match: re.Match) -> str:
        expressions.append(match.group(0))
        return f"\x00{len(expressions) - 1}\x01"
    text = GITHUB_EXPRESSION.sub(hold, script)
    if "${{" in text:
        raise LockError("an unterminated ${{ expression")
    commands: list[list[Word]] = []
    _Bash(text, expressions, commands).script(stop=None)
    return [command for command in commands if command]


ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=", re.S)
SHELLS = {"bash", "sh", "dash", "zsh", "ksh", "mksh", "ash"}
NESTING = 8


# Builtins and programs that run the command named after their own options: (the options that
# take the next word as their value, the words they read between their options and that command).
WRAPPERS = {
    "builtin": (set(), 0),
    "command": (set(), 0),
    "doas": ({"-u", "-C"}, 0),
    "env": ({"-u", "--unset", "-C", "--chdir"}, 0),
    "exec": ({"-a"}, 0),
    "nice": ({"-n", "--adjustment"}, 0),
    "nohup": (set(), 0),
    "setsid": (set(), 0),
    "stdbuf": ({"-i", "--input", "-o", "--output", "-e", "--error"}, 0),
    "sudo": ({"-u", "--user", "-g", "--group", "-C", "--close-from", "-D", "--chdir", "-h", "--host", "-p",
              "--prompt", "-r", "--role", "-t", "--type", "-T", "--command-timeout", "-U", "--other-user"}, 0),
    "time": ({"-f", "--format", "-o", "--output"}, 0),
    "timeout": ({"-k", "--kill-after", "-s", "--signal"}, 1),
    "xargs": ({"-a", "--arg-file", "-d", "--delimiter", "-E", "-e", "--eof", "-I", "--replace", "-L",
               "--max-lines", "-n", "--max-args", "-P", "--max-procs", "-s", "--max-chars",
               "--process-slot-var"}, 0),
}


def _named(words: list[Word]) -> tuple[list[Word], int | None, bool]:
    """A command's words other than its standard input; where the command it runs is named
    among them (after assignments, reserved words and the WRAPPERS with their options), or None
    when nothing runs there (no command, or `command -v`/`-V`, which looks a command up); and
    whether that is certain. It is not when a word holding an expansion stands where a wrapper
    could (`"$SUDO" bash -c ...`) and a shell's name follows it."""
    args = [w for w in words if not w.stdin]
    index = 0
    while index < len(args):
        text = args[index].text
        if ASSIGNMENT.match(text) or text in RESERVED_WORDS:
            index += 1
            continue
        if args[index].dynamic:
            following = args[index + 1].text if index + 1 < len(args) else ""
            if Path(following).name in SHELLS:
                return args, index + 1, False
            return args, index, True
        name = Path(text).name
        if name not in WRAPPERS:
            return args, index, True
        takes_value, positional = WRAPPERS[name]
        index += 1
        while index < len(args):
            option = args[index].text
            if option == "--":
                index += 1
                break
            if option in takes_value:
                index += 2
            elif len(option) > 1 and option.startswith("-"):
                short = not option.startswith("--")
                if name == "command" and short and re.search(r"[vV]", option):
                    return args, None, True  # a lookup: nothing runs
                if name == "env" and ((short and "S" in option) or option.startswith("--split-string")):
                    raise LockError("env -S, whose string becomes the command, which this check does not read")
                index += 1
            else:
                break
        index += positional  # timeout's duration
    return args, None, True


def _nested_scripts(words: list[Word]) -> list[str]:
    """The scripts a command runs as bash: an `eval`'s words, a shell's `-c` (or `+c`) argument,
    or the here-document or here-string a shell reads, the command found at its command
    position. `source` and `.`, a script built at run time, and a shell reading its script from
    a pipe are refused; a script file is not read."""
    args, index, certain = _named(words)
    if index is None:
        return []
    name = args[index].text
    if name in ("source", "."):
        raise LockError(f"{name} runs a file's text as the script, which this check does not read")
    if name == "eval":
        rest = args[index + 1:]
        if rest and rest[0].text == "--":
            rest = rest[1:]  # eval's own end of options
        if any(w.dynamic for w in rest):
            raise LockError("eval of text built at run time, which this check does not read")
        return [" ".join(w.text for w in rest)]
    shell = Path(name).name
    if shell not in SHELLS:
        return []
    stdin = [w for w in words if w.stdin]
    k, from_option, from_stdin = index + 1, False, False
    while k < len(args):
        text = args[k].text
        if text in ("--", "-"):
            k += 1
            break
        if text in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
            k += 2
        elif len(text) > 1 and text[0] in "-+":
            if not text.startswith("--"):
                from_option |= "c" in text[1:]  # bash runs the string after `+c` too
                from_stdin |= "s" in text[1:]
            k += 1
        else:
            break
    if from_option:
        if k >= len(args) or args[k].dynamic:
            raise LockError(f"{shell} of text built at run time, which this check does not read")
        return [args[k].text]
    if k < len(args) and not from_stdin:
        return []  # a script file (`bash x.sh`, `bash <(curl ...)`): not read, as the pull request says
    if not stdin:
        if not certain:
            return []
        raise LockError(f"{shell} reading its script from a pipe or a file, which this check does not read")
    if any(w.dynamic for w in stdin):
        raise LockError(f"{shell} of text built at run time, which this check does not read")
    return [w.text for w in stdin]


def script_commands(script: str) -> list[list[Word]]:
    """Every simple command a `run:` value runs: its own, and those of every script it gives to
    `eval` or to a shell, read in turn."""
    pending = [(command, 0) for command in shell_commands(script)]
    commands: list[list[Word]] = []
    while pending:
        command, depth = pending.pop(0)
        commands.append(command)
        for text in _nested_scripts(command):
            if depth >= NESTING:
                raise LockError("scripts nested deeper than this check reads")
            pending.extend((sub, depth + 1) for sub in shell_commands(text))
    return commands


def _resolve(path: str, env: dict[str, set[str]]) -> str:
    def lookup(match: re.Match) -> str:
        values = env.get(match.group(1) or match.group(2), set())
        if len(values) != 1:
            raise LockError(f"${match.group(1) or match.group(2)} has {len(values)} values in the job")
        return next(iter(values))
    resolved = re.sub(r"\$\{(\w+)\}|\$(\w+)", lookup, path)
    resolved = re.sub(r"^\$\{\{\s*github\.workspace\s*\}\}/", "", resolved)
    if "$" in resolved or resolved.startswith("/"):
        raise LockError(f"requirements path {path!r} does not resolve inside the repository")
    return Path(resolved).as_posix()


def _pip_install_args(command: list[str]) -> list[str] | None:
    """The arguments after `install` when the command is a pip install (`pip install`, `pip3
    install`, `python -m pip install`, `python -mpip install`, `uv pip install`), else None."""
    for index, token in enumerate(command):
        if token != "install" or index == 0:
            continue
        before = command[index - 1]
        if Path(before).name in ("pip", "pip3") or before == "-mpip":
            return command[index + 1:]
    return None


def _installer_before_pip(command: list[str]) -> str | None:
    """An installer program that runs this pip install (`uvx pip install`, `pipx run pip
    install`, `uv run pip install`), which may install packages of its own; `uv pip install` is
    uv's own pip, a spelling this check reads."""
    at = next(index for index, token in enumerate(command) if token == "install" and index
              and (Path(command[index - 1]).name in ("pip", "pip3") or command[index - 1] == "-mpip"))
    before = command[:at - 1]
    if before and Path(before[-1]).name == "uv":
        before = before[:-1]
    return next((token for token in before if INSTALLER_PROGRAM.fullmatch(token)), None)


def _requirement_files(args: list[str], env: dict[str, set[str]]) -> list[str]:
    files = []
    index = 0
    while index < len(args):
        token = args[index]
        if token in ("-r", "--requirement"):
            if index + 1 >= len(args):
                raise LockError(f"{token} without a file")
            files.append(_resolve(args[index + 1], env))
            index += 2
            continue
        if token.startswith("--requirement="):
            files.append(_resolve(token.split("=", 1)[1], env))
        elif token.startswith("-r") and len(token) > 2:
            files.append(_resolve(token[2:], env))
        elif token not in ALLOWED_PIP_OPTIONS:
            raise LockError(f"installs {token!r}, which is not a hash-locked requirements file"
                            if not token.startswith("-") else f"pip option {token!r} is not allowed")
        index += 1
    if not files:
        raise LockError("a pip install with no requirements file")
    return files


SIMPLE_PARAMETER = re.compile(r"\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)\})")


def shell_variables(commands: list[list[Word]], env: dict[str, set[str]]) -> dict[str, set[str] | None]:
    """The literal values each variable is given by the job's `env:` or an assignment in the
    script; None for a variable that is also given a value known only at run time."""
    values: dict[str, set[str] | None] = {name: (None if any("${{" in t for t in texts) else set(texts))
                                          for name, texts in env.items()}
    for words in commands:
        for word in words:
            match = None if word.stdin else ASSIGNMENT.match(word.text)
            if not match:
                continue
            name = word.text[:match.end()].rstrip("+=")
            if word.dynamic or word.text[match.end() - 2] == "+":
                values[name] = None
            elif values.get(name, set()) is not None:
                values.setdefault(name, set()).add(word.text[match.end():])
    return values


def _substitutions(text: str, variables: dict[str, set[str] | None]) -> list[str] | None:
    """Each text a word holding only `$NAME` and `${NAME}` expansions can become, or None."""
    names = [m.group(1) or m.group(2) for m in SIMPLE_PARAMETER.finditer(text)]
    rest = SIMPLE_PARAMETER.sub("", text)
    if "$" in rest or "`" in rest or GLOB_OR_BRACE.search(text) or any(variables.get(n) is None for n in names):
        return None
    options = [text]
    for name in dict.fromkeys(names):
        options = [o.replace(f"${{{name}}}", v).replace(f"${name}", v) for o in options for v in sorted(variables[name])]
        if len(options) > 64:
            return None
    return options


def _literal_refusal(texts: list[str]) -> str | None:
    if INSTALLER.search(" ".join(texts)):
        return "an installer this check does not read"
    for index, text in enumerate(texts):
        value = text[ASSIGNMENT.match(text).end():] if ASSIGNMENT.match(text) else text
        if (INSTALLER_PROGRAM.fullmatch(value) or text == "-mpip"
                or (text == "-m" and texts[index + 1:index + 2] in (["pip"], ["pip3"]))):
            return "an installer program run in a way this check does not read"
    return None


def unread_command(words: list[Word], variables: dict[str, set[str] | None]) -> str | None:
    """Why a simple command that is no readable pip install may still install packages, or
    None. Its words are read after quote removal; `$NAME` expansions are tried with every
    literal value the script or the job gives them; other run-time values are not read."""
    texts = [w.text for w in words]
    why = _literal_refusal(texts)
    if why or not any(w.dynamic for w in words):
        return why
    options: list[list[str]] = [[]]
    unresolved = []
    for index, word in enumerate(words):
        values = _substitutions(word.text, variables) if word.dynamic else [word.text]
        if values is None:
            base = word.text.rsplit("/", 1)[-1]
            if not ("/" in word.text and base and not re.search(r"[$`]", base) and not GLOB_OR_BRACE.search(base)):
                unresolved.append(index)  # a program under an unknown folder is still known by its name
            values = [word.text]
        options = [o + (v.split() if word.dynamic else [v]) for o in options for v in values]
        if len(options) > 64:
            return "a command whose expansions this check cannot all try"
    verb = next((i for i, t in enumerate(texts) if t in INSTALLER_VERBS), None)
    if verb is not None and any(i < verb for i in unresolved):
        return "an installer verb after an expansion this check cannot resolve"
    for option in options:
        if _pip_install_args(option) is not None:
            return "a pip install through a variable, which this check does not read"
        why = _literal_refusal(option)
        if why:
            return f"{why} (through a variable)"
    return None


def _job_of(path: tuple) -> str | None:
    return path[1] if len(path) > 2 and path[0] == "jobs" and isinstance(path[1], str) else None


def workflow_installs(source: str, text: str) -> tuple[list[Install], list[str]]:
    """Every pip install in a workflow and the job it runs in, read as YAML and bash read the
    file (see the comment above `read_yaml`). Every form the check does not read, every step
    that uses an action it has not read, every command that may install packages but is no
    pip install of requirements files, and installer text in any value other than `run:`, is a
    problem, never "nothing installed"."""
    installs: list[Install] = []
    scalars, unread = read_yaml(text)
    problems = [f"{source}:{line + 1}: {why}" for line, why in unread]
    by_path = {scalar.path: scalar.text for scalar in scalars}
    settings: dict[str, tuple[str | None, set[str], dict[str, set[str]]]] = {}
    for scalar in scalars:
        job = _job_of(scalar.path)
        if job is None:
            continue
        _, versions, env = settings.setdefault(job, (by_path.get(("jobs", job, "runs-on")), set(), {}))
        key = scalar.path[-1]
        if key == "python-version":
            versions.add(scalar.text)
        elif isinstance(key, str) and ENV_KEY.fullmatch(key):
            env.setdefault(key, set()).add(scalar.text)
    for scalar in scalars:
        path, key, job = scalar.path, scalar.path[-1] if scalar.path else None, _job_of(scalar.path)
        line = f"{source}:{scalar.line + 1}"
        if key == "uses":
            name = scalar.text.split("@", 1)[0]
            reusable = len(path) == 3 and re.fullmatch(r"\./\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", scalar.text)
            if not reusable and not (len(path) == 5 and path[2] == "steps" and name in ACTIONS):
                problems.append(f"{line}: uses {scalar.text!r}, which this check has not read (add an action to "
                                "ACTIONS only after reading what it installs)")
            continue
        if key != "run":
            if INSTALLER.search(scalar.text):
                what = f"a `{key}:` value" if isinstance(key, str) else "a list entry"
                problems.append(f"{line}: installer text in {what} this check does not read")
            continue
        try:
            commands = script_commands(scalar.text)
        except LockError as exc:
            problems.append(f"{line}: a run step this check cannot read as bash: {exc}")
            continue
        if job is None:
            variables = shell_variables(commands, {})
            if any(_pip_install_args([w.text for w in words]) is not None or unread_command(words, variables)
                   for words in commands):
                problems.append(f"{line}: a run step that may install packages sits outside any job this check "
                                "can read")
            continue
        runner_label, versions, env = settings[job]
        variables = shell_variables(commands, env)
        shell = next((by_path[p] for p in (path[:-1] + ("shell",), ("jobs", job, "defaults", "run", "shell"),
                                           ("defaults", "run", "shell")) if p in by_path),
                     "pwsh" if RUNNERS.get(runner_label or "", ("",))[0] == "windows" else "bash")
        where = f"{source} job {job}"
        for words in commands:
            texts = [w.text for w in words]
            try:
                args = _pip_install_args(texts)
                if args is None:
                    why = unread_command(words, variables)
                    if why:
                        raise LockError(f"{why}: {' '.join(texts)!r}")
                    continue
                runner_program = _installer_before_pip(texts)
                if runner_program:
                    raise LockError(f"a pip install run through {runner_program!r}, which may install packages itself")
                if shell.split()[:1] != ["bash"]:
                    raise LockError(f"a pip install in a step whose shell is {shell!r}; this check reads bash")
                files = _requirement_files(args, env)
                if runner_label not in RUNNERS:
                    raise LockError(f"runner {runner_label!r} is not in RUNNERS")
                if len(versions) != 1:
                    raise LockError(f"the job sets python-version {sorted(versions)} (exactly one is needed)")
                target = Target(runner_label, next(iter(versions)))
                target.python_match()
                installs.extend(Install(source, job, file, target) for file in files)
            except (LockError, ValueError) as exc:
                problems.append(f"{where}: {exc}")
    return installs, problems


# --- proposals: the workflows as the patches would leave them -----------------------
# Patches are always applied to LF copies in a scratch folder, never to the working tree:
# a Windows checkout may hold CRLF files, and an empty context line of a patch would then
# be a lone CR, which git apply refuses.
def patch_paths(patch: str) -> list[str]:
    paths: list[str] = []
    for line in patch.splitlines():
        found = re.match(r"^diff --git a/(\S+) b/(\S+)$", line)
        names = found.groups() if found else re.findall(r"^(?:---|\+\+\+) [ab]/(\S+)$", line)
        for name in names:
            if name not in paths:
                paths.append(name)
    return paths


def lf_copy(root: Path, rels, scratch: Path) -> Path:
    for rel in rels:
        source = root / rel
        if source.is_file():
            (scratch / rel).parent.mkdir(parents=True, exist_ok=True)
            (scratch / rel).write_bytes(source.read_bytes().replace(b"\r\n", b"\n"))
    return scratch


def git_apply(root: Path, patch: Path, *args: str) -> subprocess.CompletedProcess:
    """git apply in root, which must lie outside any repository (git then acts like patch)."""
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(root.resolve().parent))
    return subprocess.run(["git", "apply", *args, "-"], cwd=root, env=env, capture_output=True,
                          input=patch.read_bytes().replace(b"\r\n", b"\n"), check=False)


def proposal_state(root: Path, patch: Path) -> str:
    """"applies", "applied" (already in the tree) or "conflicts", judged on an LF copy."""
    with tempfile.TemporaryDirectory(prefix="hash-lock-state-") as scratch:
        copy = lf_copy(root, patch_paths(patch.read_text(encoding="utf-8")), Path(scratch))
        if git_apply(copy, patch, "--check").returncode == 0:
            return "applies"
        if git_apply(copy, patch, "--check", "--reverse").returncode == 0:
            return "applied"
    return "conflicts"


def proposed_tree(root: Path, patch: Path, scratch: Path) -> Path:
    """Copy the files a patch touches into scratch and apply it there."""
    lf_copy(root, patch_paths(patch.read_text(encoding="utf-8")), scratch)
    result = git_apply(scratch, patch)
    if result.returncode:
        raise LockError(f"{patch.name} does not apply to a copy: {result.stderr.decode(errors='replace').strip()}")
    return scratch


def all_installs(root: Path) -> tuple[list[Install], list[str]]:
    installs: list[Install] = []
    problems: list[str] = []
    workflows = root / WORKFLOWS
    for path in sorted(workflows.glob("*.y*ml")):
        found, bad = workflow_installs(path.name, path.read_text(encoding="utf-8"))
        installs += found
        problems += bad
    gate = root / GATE
    if gate.is_file() and INSTALLER.search(gate.read_text(encoding="utf-8")):
        problems.append(f"{GATE} now installs something; add it to this check")
    for patch in sorted((root / PROPOSALS).glob("*.patch")):
        if not any(p.startswith(WORKFLOWS + "/") for p in patch_paths(patch.read_text(encoding="utf-8"))):
            continue
        state = proposal_state(root, patch)
        if state == "applied":
            continue  # its workflows are the tree's, read above
        if state == "conflicts":
            problems.append(f"{PROPOSALS}/{patch.name} neither applies to the tree nor is applied in it")
            continue
        with tempfile.TemporaryDirectory(prefix="hash-lock-") as scratch:
            try:
                tree = proposed_tree(root, patch, Path(scratch))
            except LockError as exc:
                problems.append(str(exc))
                continue
            for path in sorted((tree / WORKFLOWS).glob("*.y*ml")):
                found, bad = workflow_installs(f"{path.name} (as {patch.name} leaves it)",
                                               path.read_text(encoding="utf-8"))
                installs += found
                problems += bad
    return installs, problems


# --- the record and the check ----------------------------------------------------------
@dataclass
class Report:
    problems: list[str] = field(default_factory=list)
    files: int = 0
    pins: int = 0
    targets: set[Target] = field(default_factory=set)


def load_record(root: Path) -> dict:
    record = json.loads((root / RECORD).read_text(encoding="utf-8"))
    if record.get("format") != RECORD_FORMAT or not isinstance(record.get("files"), dict):
        raise LockError(f"{RECORD}: format {record.get('format')!r}, expected {RECORD_FORMAT} with a files map")
    return record


def check_file(rel: str, text: str, entry: dict, targets: set[Target], report: Report) -> None:
    try:
        pins = parse_requirements(text)
    except LockError as exc:
        report.problems.append(f"{rel}: {exc}")
        return
    report.pins += len(pins)
    recorded = entry.get("pins", {})
    by_name = {pin.name: pin for pin in pins}
    if set(by_name) != set(recorded):
        report.problems.append(f"{rel}: pins {sorted(by_name)} differ from the record's {sorted(recorded)}")
    for pin in pins:
        rec = recorded.get(pin.name)
        if rec is None:
            continue
        if (rec.get("version"), rec.get("marker")) != (pin.version, pin.marker):
            report.problems.append(f"{rel}:{pin.line}: {pin.name}=={pin.version} ; {pin.marker} differs from "
                                   f"the record ({rec.get('version')} ; {rec.get('marker')})")
        dists = rec.get("dists", [])
        for dist in dists:
            if dist_release(dist.get("filename", "")) != (pin.name, pin.version):
                report.problems.append(f"{rel}:{pin.line}: the record lists {dist.get('filename')!r} for "
                                       f"{pin.name}=={pin.version}; its name says another release")
        recorded_hashes = {d.get("sha256") for d in dists}
        if pin.hashes != recorded_hashes:
            missing = sorted(d["filename"] for d in dists if d.get("sha256") not in pin.hashes)
            extra = sorted(pin.hashes - recorded_hashes)
            report.problems.append(f"{rel}:{pin.line}: {pin.name} hashes differ from the record "
                                   f"(missing for {missing}; not recorded: {extra})")
    for target in sorted(targets, key=str):
        env = target.env()
        applying = {pin.name: pin for pin in pins if marker_applies(pin.marker, env)}
        for name, pin in applying.items():
            if name not in recorded:
                continue  # already reported above
            rec = recorded[name]
            where = f"{rel}: {name}=={pin.version} on {target}"
            requires_python = rec.get("requires_python") or ""
            if not satisfies(f"3.{target.minor}.0", requires_python):
                report.problems.append(f"{where}: requires Python {requires_python}")
            wheels = [d["filename"] for d in rec.get("dists", [])
                      if d.get("sha256") in pin.hashes and wheel_installs(d["filename"], target)
                      and satisfies(f"3.{target.minor}.0", d.get("requires_python") or "")]
            if not wheels:
                report.problems.append(f"{where}: no hashed wheel installs there (--only-binary=:all:)")
            for requirement in rec.get("requires_dist", []):
                try:
                    # The marker first: an entry for an extra CI never asks for does not apply.
                    if not marker_applies(dependency_marker(requirement), env):
                        continue
                    dep, spec, _ = parse_dependency(requirement)
                except LockError as exc:
                    report.problems.append(f"{where}: {exc}")
                    continue
                if dep not in applying:
                    report.problems.append(f"{where}: needs {requirement!r}, which the file does not pin there")
                elif not satisfies(applying[dep].version, spec):
                    report.problems.append(f"{where}: needs {requirement!r}; the file pins "
                                           f"{dep}=={applying[dep].version}")


def check(root: Path = ROOT) -> Report:
    report = Report()
    installs, problems = all_installs(root)
    report.problems += problems
    try:
        record = load_record(root)
    except (OSError, ValueError) as exc:
        report.problems.append(f"{RECORD}: {exc}")
        return report
    targets: dict[str, set[Target]] = {}
    for install in installs:
        targets.setdefault(install.file, set()).add(install.target)
        report.targets.add(install.target)
    for rel in sorted(set(targets) - set(record["files"])):
        users = sorted({f"{i.source} job {i.job}" for i in installs if i.file == rel})
        report.problems.append(f"{rel} is installed by {users} but is not hash-locked in {RECORD}")
    for rel in sorted(set(record["files"]) - set(targets)):
        report.problems.append(f"{RECORD} locks {rel}, which no workflow or proposal installs")
    for rel in sorted(set(targets) & set(record["files"])):
        path = root / rel
        if not path.is_file():
            report.problems.append(f"{rel}: installed but missing from the tree")
            continue
        report.files += 1
        try:
            check_file(rel, path.read_text(encoding="utf-8"), record["files"][rel], targets[rel], report)
        except LockError as exc:
            report.problems.append(f"{rel}: {exc}")
    return report


def main() -> int:
    if Path.cwd().resolve() != ROOT.resolve():
        print(f"run from the repository root ({ROOT})", file=sys.stderr)
        return 2
    report = check(ROOT)
    for problem in report.problems:
        print(f"HASH_LOCK_PROBLEM {problem}")
    if report.problems:
        print(f"HASH_LOCK_FAIL problems={len(report.problems)}")
        return 1
    print(f"HASH_LOCK_PASS files={report.files} pins={report.pins} targets={len(report.targets)}: "
          + ", ".join(sorted(str(t) for t in report.targets)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
