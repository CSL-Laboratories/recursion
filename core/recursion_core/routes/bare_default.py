"""Read build help for print-mode bare defaults and a documented auth-mode opt-out.

Unknown or incomplete help fails closed. The subscription route runs as beta only
when ``claude --help`` matches the one measured hash and the build is at or above
the 2.1.314 floor (CSLR-D-011, PRODUCT.md 055cc7a5c793b7135e5d0565dc4198cf96230531).
A different help text names the build and refuses until it is re-measured. Unknown
is not beta. Only ``--help`` and ``--version`` are executed; no provider call,
settings edit or credential lookup is performed.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping

STATES = ("absent", "present", "unknown")
HELP_TIMEOUT_S = 10.0
HELP_MAX_BYTES = 1 << 20
# One measured help text. Builds 2.1.314 through 2.1.321 share this hash, so the
# table is one entry. It is not a per-version allow list. Classifier "unknown" is
# not an entry and is not an allow.
MEASURED_HELP = (
    {"help_sha256": "5ca3a9dac2026e56c221fdd34380c7d7d45ebbb51c1d01b7c17c75cb076a0be8",
     "label": "beta"},
)
HELP_FLOOR = (2, 1, 314)

# An option header is indented exactly two spaces or one tab and starts with a flag.
# Every long name on that header shares the block, including comma and pipe aliases
# and a ``=<type>`` suffix. Wrapped description lines are indented further.
_HEADER = re.compile(r"^(?:  |\t)(?=-)", re.MULTILINE)
_LONG = re.compile(r"--[a-z0-9][a-z0-9-]*")
_FLAG_GAP = re.compile(r" {2,}|\t")
# A component longer than this is not a build number. int() raises past 4300 digits,
# and a shorter oversized token would be copied into the refusal.
_BUILD_PART_MAX = 9
# Present-tense "default for -p" wording in the --bare block. A future-release clause
# drops that claim only when the clause itself is the bare-default statement.
_DEFAULT_NOW = re.compile(
    r"(?:\bis\s+(?:now\s+)?(?:the\s+)?default\b[^.]*(?:-p\b|--print\b|print\s+mode|headless)"
    r"|\b(?:on|enabled)\s+by\s+default\b[^.]*(?:-p\b|--print\b|print\s+mode|headless)"
    r"|\bdefault\s+(?:for|in|with|under)\s+(?:-p\b|--print\b|print\s+mode|headless))",
    re.IGNORECASE)
# Inside the --bare block the subject is bare itself, so a bare "this is the default" is enough.
_BARE_SELF_DEFAULT = re.compile(
    r"\b(?:is|becomes)\s+(?:now\s+)?(?:the\s+)?default\b|\(default(?:\s*[:=]\s*(?:true|on|enabled))?\)"
    r"|\b(?:on|enabled)\s+by\s+default\b|\bdefault\s*[:=]\s*(?:true|on|enabled)\b"
    r"|\bdefaults\s+to\s+(?:on|true|enabled)\b",
    re.IGNORECASE)
# Inside the -p/--print block: the sentence must name bare and say it is the default now.
_PRINT_BARE_DEFAULT = re.compile(
    r"\bbare\b[^.;]*?\b(?:is|becomes)\s+(?:now\s+)?(?:the\s+)?default\b"
    r"|\bbare\b[^.;]*?\(default(?:\s*[:=]\s*(?:true|on|enabled))?\)"
    r"|\bbare\b[^.;]*?\b(?:by\s+default|default\s+mode)\b"
    r"|\bdefaults?\s+(?:to|is)\s+(?:the\s+)?(?:--)?bare\b",
    re.IGNORECASE)
# A future clause governs the bare default only when it is the claim about that default.
_FUTURE_GOVERNS = re.compile(
    r"\bwill\s+(?:become|be)\b[^.]{0,80}\bdefault\b"
    r"|\b(?:is|becomes)\s+(?:now\s+)?(?:the\s+)?default\b[^.]{0,80}"
    r"\b(?:in\s+a\s+future|future\s+release|upcoming|planned)\b"
    r"|\bdefault\s+(?:for|in|with|under)\s+(?:-p\b|--print\b|print\s+mode|headless)\b[^.]{0,80}"
    r"\b(?:in\s+a\s+future|future\s+release)\b"
    r"|\b(?:upcoming|planned)\s+(?:to\s+be\s+)?(?:the\s+)?default\b",
    re.IGNORECASE)
_CLAUSE = re.compile(r",")
# Negated or conditional wording never proves the default either way.
_NEGATED = re.compile(r"\bnot\b|\bno\s+longer\b|\bnever\b|\bunless\b|\bexcept\b|\bwithout\b|n't\b",
                      re.IGNORECASE)
# An option that turns bare mode off would only exist if bare can be on without the flag.
_OPT_OUT = ("--no-bare",)


def _option_raw_blocks(text: str) -> dict[str, str]:
    """Map each long option to its raw help lines (the option line plus its wrapped description).

    A later header for the same flag is kept. The first block must not hide a later one.
    """

    if not isinstance(text, str) or not text:
        return {}
    headers = list(_HEADER.finditer(text))
    parts: dict[str, list[str]] = {}
    for i, header in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        raw = text[header.start():end]
        content = raw.split("\n", 1)[0].lstrip(" \t")
        gap = _FLAG_GAP.search(content)
        flag_area = content[:gap.start()] if gap else content
        for name in _LONG.findall(flag_area):
            parts.setdefault(name, []).append(raw)
    return {name: "".join(chunks) for name, chunks in parts.items()}


def _option_blocks(text: str) -> dict[str, str]:
    """Each long option's help block joined onto one line."""

    return {opt: " ".join(raw.split()) for opt, raw in _option_raw_blocks(text).items()}


def _sentences(block: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.;])\s+", block) if s]


def _affirmed(pattern: re.Pattern[str], sentence: str) -> bool:
    """True if ``pattern`` matches with no negation in the match or the few words before it."""

    for m in pattern.finditer(sentence):
        if not _NEGATED.search(sentence[max(0, m.start() - 24):m.end()]):
            return True
    return False


def _clauses(sentence: str) -> list[str]:
    return [part.strip() for part in _CLAUSE.split(sentence) if part.strip()]


def _future_governs_clause(clause: str) -> bool:
    """True when this clause's future-release wording is the claim about the default."""

    for match in _FUTURE_GOVERNS.finditer(clause):
        if not _NEGATED.search(clause[max(0, match.start() - 24):match.end()]):
            return True
    return False


def _sentence_default(sentence: str, patterns: tuple) -> str | None:
    """How one help sentence speaks about the bare default.

    ``present`` is a present-tense default the future wording does not govern.
    ``unknown`` is a present claim and a separate future claim about that default.
    ``absent`` is only a future claim that bare will be the default.
    ``governed`` is future wording that must not be promoted to present.
    """

    present = False
    governed = False
    for clause in _clauses(sentence):
        if _future_governs_clause(clause):
            governed = True
        elif any(_affirmed(pattern, clause) for pattern in patterns):
            present = True
    if present and governed:
        return "unknown"
    if present:
        return "present"
    if governed and "bare" in sentence.lower() and _DEFAULT_NOW.search(sentence):
        return "absent"
    if governed:
        return "governed"
    return None


def classify_help(text: object) -> str:
    """Return ``"absent"``, ``"present"`` or ``"unknown"`` for one build's ``claude --help`` text."""

    if not isinstance(text, str) or not text.strip():
        return "unknown"
    blocks = _option_blocks(text)
    if "--bare" not in blocks or "--print" not in blocks:
        return "unknown"
    if any(opt in blocks for opt in _OPT_OUT):
        return "present"
    absent = False
    ambiguous = False
    for option in ("--bare", "--print"):
        # Either block saying --bare is the default now is enough: print's own help may announce
        # it even when the --bare line does not.
        patterns = (_PRINT_BARE_DEFAULT,) if option == "--print" else (_DEFAULT_NOW, _BARE_SELF_DEFAULT)
        for sentence in _sentences(blocks[option]):
            decision = _sentence_default(sentence, patterns)
            if decision == "present":
                return "present"
            if decision == "unknown":
                ambiguous = True
            elif decision == "absent":
                absent = True
            elif decision != "governed" and any(_affirmed(p, sentence) for p in patterns):
                return "present"
            if option == "--bare" and re.search(r"default\s*[:=]?\s*(?:false|off|disabled)\b",
                                                sentence, re.I):
                absent = True
    if ambiguous:
        return "unknown"
    return "absent" if absent else "unknown"


def auth_mode(text: object) -> dict:
    """Route metadata, including only an opt-out actually advertised by this build."""
    state = classify_help(text)
    blocks = _option_blocks(text) if isinstance(text, str) else {}
    return {"bare_default": state,
            "bare_opt_out": next((flag for flag in _OPT_OUT if flag in blocks), None)}


def help_sha256(text: str) -> str:
    """SHA-256 of help text as UTF-8, the same bytes the measured fixtures commit."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_build(build: object) -> str | None:
    """``major.minor.patch`` from a version string, or None when the build is unknown.

    ``2.1.321 (Claude Code)`` is ``2.1.321``. A suffix on the version token
    (``2.1.321-dev``) is unknown: unknown is not treated as a measured build.
    """
    if not isinstance(build, str) or not build.strip():
        return None
    token = build.strip().split()[0]
    if not re.fullmatch(r"\d+\.\d+\.\d+", token):
        return None
    return token


def _build_key(version: str) -> tuple[int, int, int] | None:
    parts = version.split(".")
    if len(parts) != 3 or any(len(part) > _BUILD_PART_MAX for part in parts):
        return None
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None


def _help_utf8(help_text: object) -> bytes | None:
    """UTF-8 bytes of help text, or None when it is missing, too long, or not encodable.

    The text itself is not copied into the refusal. A lone surrogate raises
    ``UnicodeEncodeError``; that exception's ``repr`` would include the text.
    """

    if not isinstance(help_text, str) or help_text == "":
        return None
    try:
        raw = help_text.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(raw) > HELP_MAX_BYTES:
        return None
    return raw


def subscription_beta(build: object, help_text: object) -> dict:
    """Admit the Claude subscription route as beta, or refuse.

    Beta only when ``help_text`` hashes to the single ``MEASURED_HELP`` entry and
    the build is at or above ``HELP_FLOOR`` (2.1.314). Help that differs names the
    build and says it needs re-measuring. A build below the floor refuses even
    when the hash matches. Missing or unreadable build or help refuses. Unknown
    is not beta, and there is no per-version allow list.
    """
    version = parse_build(build)
    if version is None:
        return {"disposition": "refuse", "label": None, "build": None, "help_sha256": None,
                "detail": "claude build is unknown; unknown is not safe"}
    key = _build_key(version)
    if key is None:
        return {"disposition": "refuse", "label": None, "build": None, "help_sha256": None,
                "detail": "claude build is unknown; unknown is not safe"}
    if key < HELP_FLOOR:
        return {"disposition": "refuse", "label": None, "build": version, "help_sha256": None,
                "detail": f"claude {version} is below the 2.1.314 floor"}
    raw = _help_utf8(help_text)
    if raw is None:
        return {"disposition": "refuse", "label": None, "build": version, "help_sha256": None,
                "detail": f"claude {version} help is unknown; unknown is not safe"}
    digest = hashlib.sha256(raw).hexdigest()
    entry = next((row for row in MEASURED_HELP if row["help_sha256"] == digest), None)
    if entry is None:
        return {"disposition": "refuse", "label": None, "build": version, "help_sha256": digest,
                "detail": (f"claude {version} help does not match the measured fixture; "
                           "this build needs re-measuring")}
    return {"disposition": "beta", "label": entry["label"], "build": version,
            "help_sha256": digest, "detail": entry["label"]}


Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


def _capture(binary: str, flag: str, parent_env: Mapping[str, str], run: Runner) -> str | None:
    """Run ``<binary> <flag>`` once. The child gets only ``PATH``, ``HOME`` and ``LANG``."""
    env = {k: parent_env[k] for k in ("PATH", "HOME", "LANG") if isinstance(parent_env.get(k), str)}
    try:
        done = run([binary, flag], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                   stderr=subprocess.DEVNULL, timeout=HELP_TIMEOUT_S, check=False, shell=False)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    out = done.stdout
    if done.returncode != 0 or not isinstance(out, bytes) or len(out) > HELP_MAX_BYTES:
        return None
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return None


def read_help(binary: str, parent_env: Mapping[str, str], run: Runner = subprocess.run) -> str | None:
    """Run ``<binary> --help`` once and return its text, or ``None`` if it cannot be read.

    The child gets only ``PATH``, ``HOME`` and ``LANG`` from the explicit ``parent_env`` (never
    ``os.environ``), so no credential variable reaches it. A non-zero exit, timeout, launch failure or
    non-UTF-8 output is ``None`` (classified ``"unknown"``).
    """
    return _capture(binary, "--help", parent_env, run)


def read_version(binary: str, parent_env: Mapping[str, str], run: Runner = subprocess.run) -> str | None:
    """Run ``<binary> --version`` once under the same limit as ``read_help``, or ``None``."""
    return _capture(binary, "--version", parent_env, run)


def probe(binary: str, parent_env: Mapping[str, str], run: Runner = subprocess.run) -> str:
    """``classify_help(read_help(...))``: the value a surface reports as ``route.bare_default``."""

    return classify_help(read_help(binary, parent_env, run))


def probe_auth_mode(binary: str, parent_env: Mapping[str, str], run: Runner = subprocess.run) -> dict:
    """Read help once and report the explicit subscription auth-mode capability."""
    return auth_mode(read_help(binary, parent_env, run))


def main(argv: list[str] | None = None, run: Runner = subprocess.run) -> int:
    """Print one build's classification and the help lines it came from.

    Runs only ``--version`` and ``--help``.
    """

    parser = argparse.ArgumentParser(prog="python -m recursion_core.routes.bare_default",
                                     description="Classify whether --bare is the -p default for one claude build.")
    parser.add_argument("--claude", required=True, help="path to the claude binary; only --version and --help are run")
    args = parser.parse_args(argv)
    version = read_version(args.claude, os.environ, run)
    text = read_help(args.claude, os.environ, run)
    metadata = auth_mode(text)
    # The adapter owns the refusal rule. Pass the build and the help text so the
    # measured-hash gate decides: beta, or a named refusal. Unknown is not beta.
    from recursion_core.routes import contract as c
    from recursion_core.routes.claude_code import ClaudeCodeAdapter
    reported = dict(metadata)
    reported["claude_build"] = version if isinstance(version, str) else ""
    reported["help_text"] = text if isinstance(text, str) else ""
    try:
        ClaudeCodeAdapter._subscription_auth_args(reported)
        route = "beta"
    except c.RouteError as exc:
        route = f"refuses ({exc})"
    print(f"binary: {args.claude}")
    print(f"build: {version if version is not None else 'unreadable'}")
    print(f"help: {'read' if text is not None else 'unreadable'}")
    print(f"bare_default: {metadata['bare_default']}")
    print(f"bare_opt_out: {metadata['bare_opt_out']}")
    print(f"subscription route: {route}")
    raw = _option_raw_blocks(text) if text is not None else {}
    for option in ("--bare", "--print", *_OPT_OUT):
        if option in raw:
            print(f"--- {option} help block:")
            print(raw[option].rstrip("\n"))
        else:
            print(f"--- {option}: not in help")
    return 0


if __name__ == "__main__":
    sys.exit(main())
