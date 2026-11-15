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

Workflow values are decoded as YAML reads them before any matching (block `|` and `>`
scalars, multi-line plain and quoted values), so a folded `run:` is read as the shell sees
it. It reads every command of every shell line (`&&`, `||`, `;`, pipes, command
substitution) and every spelling of pip it knows; a `run:` line that may install packages
but cannot be read as pip installs of requirements files, one outside a job it can read,
and installer text in any other value or in a line that is no `key: value`, are each a
problem, never "nothing installed". The gate (tools/ci_gate.py) installs nothing; the check makes
sure it stays that way.
Stdlib only and offline; tools/release/relock.py is the one script that reads PyPI.
"""
from __future__ import annotations

import json
import os
import re
import shlex
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
SHELL_OPERATORS = {"|", "||", "&", "&&", ";", ";;", "(", ")", "<", ">", ">>", "<<", "|&"}


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


JOBS_HEADER = re.compile(r"jobs:\s*(?:#.*)?")
JOB_KEY = r"([A-Za-z0-9_-]+|\"[^\"]+\"|'[^']+'):\s*(?:#.*)?"
KEY_LINE = re.compile(r"^(\s*)((?:-\s+)*)([^\s#:\-][^:#]*?|\"[^\"]*\"|'[^']*'):(?:\s+(.*?))?\s*$")
BLOCK = re.compile(r"[|>][0-9+-]*\s*(?:#.*)?")


def _jobs(lines: list[str]) -> tuple[dict[str, list[int]], list[str]]:
    """The line numbers (0-based, into the LF lines) of each job under a block-style top-level
    `jobs:`, at whatever indentation the first job uses, and what this reader cannot follow."""
    problems: list[str] = []
    headers = [i for i, line in enumerate(lines) if JOBS_HEADER.fullmatch(line)]
    if any(re.match(r"jobs:\s*[^\s#]", line) for line in lines):
        problems.append("`jobs:` is written inline; this check reads only a block mapping")
    if len(headers) != 1:
        if len(headers) > 1:
            problems.append(f"{len(headers)} top-level `jobs:` lines")
        return {}, problems
    out: dict[str, list[int]] = {}
    current: str | None = None
    indent: int | None = None
    for index in range(headers[0] + 1, len(lines)):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            if current is not None:
                out[current].append(index)
            continue
        lead = len(line) - len(line.lstrip(" "))
        if lead == 0:
            break  # the next top-level key
        if indent is None:
            indent = lead
        key = re.fullmatch(" " * indent + JOB_KEY, line) if lead == indent else None
        if key:
            current = key.group(1).strip("\"'")
            out[current] = []
        elif lead > indent and current is not None:
            out[current].append(index)
        else:
            problems.append(f"a line under `jobs:` this check cannot place in a job: {line.strip()!r}")
            current = None
    return out, problems


def _yaml_scalar(text: str) -> str:
    text = text.strip()
    if text[:1] in ("'", '"'):
        quote = text[0]
        end = text.find(quote, 1)
        while quote == "'" and end != -1 and text[end + 1:end + 2] == "'":
            end = text.find(quote, end + 2)
        if end != -1:
            inner = text[1:end]
            return inner.replace("''", "'") if quote == "'" else inner
        return text
    return re.split(r"\s+#", text, maxsplit=1)[0].strip()


def _fold(lines: list[str]) -> str:
    """YAML folding: a line break between two plain lines is a space, an empty line is a line
    break, and a more-indented line keeps its own line."""
    out = ""
    previous_plain = False
    for line in lines:
        if not line.strip():
            out += "\n"
            previous_plain = False
        elif line[:1] in (" ", "\t"):
            out += ("\n" if out and not out.endswith("\n") else "") + line + "\n"
            previous_plain = False
        else:
            out += (" " if previous_plain else "") + line
            previous_plain = True
    return out


def _scalars(lines: list[str]) -> tuple[list[tuple[int, str, str]], set[int]]:
    """Every scalar value written as `key: value`, decoded as YAML reads it: (line of the key,
    key, text). Block scalars (`|`, `>`) and multi-line plain or quoted values are joined the
    way YAML joins them. Also the set of lines those values take up."""
    found: list[tuple[int, str, str]] = []
    used: set[int] = set()
    index = 0
    while index < len(lines):
        line = lines[index]
        match = KEY_LINE.match(line)
        if not match or line.lstrip().startswith("#"):
            index += 1
            continue
        key = match.group(3).strip("\"'")
        column = len(match.group(1)) + len(match.group(2))
        value = match.group(4) or ""
        following = []
        scan = index + 1
        while scan < len(lines) and (not lines[scan].strip()
                                     or len(lines[scan]) - len(lines[scan].lstrip(" ")) > column):
            following.append(scan)
            scan += 1
        while following and not lines[following[-1]].strip():
            following.pop()
        if not value or value.startswith("#"):
            index += 1  # a mapping or sequence follows; its own keys are read in turn
            continue
        if BLOCK.fullmatch(value):
            body = [lines[i] for i in following]
            indent = min((len(l) - len(l.lstrip(" ")) for l in body if l.strip()), default=0)
            body = [l[indent:] for l in body]
            text = _fold(body) if value.startswith(">") else "\n".join(body) + "\n"
        elif following:
            text = _yaml_scalar(_fold([value] + [lines[i].strip() for i in following]))
        else:
            text = _yaml_scalar(value)
        found.append((index, key, text))
        used.update([index, *following])
        index = following[-1] + 1 if following else index + 1
    return found, used


def _shell_lines(script: str) -> list[str]:
    """Each line with its backslash continuations joined; shell comment lines dropped."""
    out: list[str] = []
    pending = ""
    for line in script.replace("\r\n", "\n").split("\n"):
        if not pending and line.lstrip().startswith("#"):
            continue
        if line.rstrip().endswith("\\"):
            pending += line.rstrip()[:-1] + " "
            continue
        out.append(pending + line)
        pending = ""
    if pending:
        out.append(pending)
    return out


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


def _commands(line: str) -> list[list[str]]:
    """The simple commands of one shell line, split at every shell operator."""
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    commands: list[list[str]] = [[]]
    for token in lexer:
        if token in SHELL_OPERATORS:
            commands.append([])
        else:
            commands[-1].append(token)
    return [command for command in commands if command]


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


def _job_settings(block: str) -> tuple[str | None, set[str], dict[str, set[str]]]:
    child = min((len(line) - len(line.lstrip(" ")) for line in block.split("\n")
                 if line.strip() and not line.lstrip().startswith("#")), default=0)
    runner = re.search(rf"(?m)^ {{{child}}}runs-on:\s*(.+?)\s*$", block)
    versions = {_yaml_scalar(v) for v in re.findall(r"(?m)^\s+python-version:\s*(.+?)\s*$", block)}
    env: dict[str, set[str]] = {}
    for key, value in re.findall(r"(?m)^\s+([A-Z_][A-Z0-9_]*):\s*(.+?)\s*$", block):
        env.setdefault(key, set()).add(_yaml_scalar(value))
    return (_yaml_scalar(runner.group(1)) if runner else None), versions, env


def workflow_installs(source: str, text: str) -> tuple[list[Install], list[str]]:
    """Every pip install in a workflow and the job it runs in. Values are decoded as YAML reads
    them before any matching, so a folded or multi-line `run:` is read as the shell sees it. A
    `run:` line that may install packages but cannot be read as pip installs of requirements
    files, one outside a job this reader found, and installer text in any other value or in a
    line that is no `key: value` at all, are each a problem."""
    installs: list[Install] = []
    lines = text.replace("\r\n", "\n").split("\n")
    jobs, layout = _jobs(lines)
    problems = [f"{source}: {problem}" for problem in layout]
    owner = {index: job for job, indexes in jobs.items() for index in indexes}
    settings = {job: _job_settings("\n".join(lines[i] for i in indexes)) for job, indexes in jobs.items()}
    scalars, used = _scalars(lines)
    for index, line in enumerate(lines):
        if index not in used and not line.lstrip().startswith("#") and INSTALLER.search(line):
            problems.append(f"{source}:{index + 1}: a line that may install packages, outside any `key: value` "
                            "this check reads")
    for index, key, value in scalars:
        if key != "run":
            if INSTALLER.search(value):
                problems.append(f"{source}:{index + 1}: installer text in a `{key}:` value this check does not read")
            continue
        job = owner.get(index)
        for line in _shell_lines(value):
            if not INSTALLER.search(line):
                continue
            if job is None:
                problems.append(f"{source}:{index + 1}: a run step that may install packages sits outside any job "
                                "this check can read")
                continue
            where = f"{source} job {job}"
            runner_label, versions, env = settings[job]
            try:
                found = False
                for simple in _commands(line.strip()):
                    args = _pip_install_args(simple)
                    if args is None:
                        if INSTALLER.search(" ".join(simple)):
                            raise LockError(f"an installer this check does not read: {' '.join(simple)!r}")
                        continue
                    found = True
                    files = _requirement_files(args, env)
                    if runner_label not in RUNNERS:
                        raise LockError(f"runner {runner_label!r} is not in RUNNERS")
                    if len(versions) != 1:
                        raise LockError(f"the job sets python-version {sorted(versions)} (exactly one is needed)")
                    target = Target(runner_label, next(iter(versions)))
                    target.python_match()
                    installs.extend(Install(source, job, path, target) for path in files)
                if not found:
                    raise LockError(f"an installer this check does not read: {line.strip()!r}")
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
