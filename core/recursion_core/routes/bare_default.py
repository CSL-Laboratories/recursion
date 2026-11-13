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
import unicodedata
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
# Present is the whole sentence, proved from every --bare chunk, any --no-bare
# block, and --print together. Cores are the only present-tense statements. A
# trailer is allowed only when the remainder of the sentence is that exact string.
# On --print a core counts only when the sentence names bare. Any other sentence
# is unknown, except the neutral allowlist and the explicit absent forms below.
_PRESENT_CORES = (
    "default: true",
    "defaults to --bare",
    "bare is the default",
    "bare is now the default",
    "bare is the default for -p",
    "bare is now the default for -p",
    "--bare is the default",
    "--bare is now the default",
    "--bare is the default for -p",
    "--bare is now the default for -p",
    "this is the default",
    "this is now the default",
    "this is the default for -p",
    "this is now the default for -p",
)
_PRESENT_TRAILERS = (
    "",
    ", not a future release",
    ", not planned for the future",
    ", and it will not be a future release",
    ", it will not be a future release",
    ", not coming soon",
    " as soon as possible",
)
_PRESENT_SENTENCES = frozenset(
    core + trailer for core in _PRESENT_CORES for trailer in _PRESENT_TRAILERS)
# Sentences that make no default claim and may sit beside a present sentence.
# "minimal mode" is the recorded short label. "print response and exit" and the
# measured print opener are the recorded neutral fragments. The rest of the
# measured 2.1.311-2.1.321 --bare description is not listed, so that help stays
# unknown.
_NEUTRAL_SENTENCES = frozenset({
    "minimal mode",
    "print response and exit",
    "print response and exit (useful for pipes)",
})
# Explicit future-default wording ("it'll be the default", "will soon be the default").
# "not" or "never" between the auxiliary and be/become is not this form.
_FUTURE_DEFAULT = re.compile(
    r"(?:it'll(?![a-z])|(?:is\s+)?going\s+to\b|(?<![a-z'])(?:will|shall)\b)"
    r"(?!\s+(?:not|never)\b)"
    r"(?:\s+(?!be\b|become\b|not\b|never\b)[a-z]+){0,4}\s+"
    r"(?:be|become)\s+the\s+default\b")
# A present-tense "is the default" beside a future claim is a contradiction, not absent.
_PRESENTISH = re.compile(
    r"\bis\s+(?:now\s+)?the\s+default\b"
    r"|\bdefaults\s+to\s+--bare\b"
    r"|\bdefault:\s*true\b")
_DEFAULT_OFF = re.compile(r"\bdefault\s*[:=]\s*(?:false|off|disabled)\b")
_NAMES_BARE = re.compile(r"\bbare\b|--bare\b")
# Printable ASCII after the whitespace collapse. A character outside this set
# that survives NFKC and quote folding fails closed.
_PRINTABLE_ASCII = frozenset(chr(code) for code in range(0x20, 0x7F))
_ASCII_SPACE = frozenset(" \t\n\r\f\v")
# Owner decision (pr336 deep6): a --no-bare header is not present by itself.
# Text under --bare and --no-bare is classified with the same whole-text rule.
# The flag is still reported as bare_opt_out.
_OPT_OUT = ("--no-bare",)


def _option_raw_chunks(text: str) -> dict[str, list[str]]:
    """Map each long option to every raw help block, in order.

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
    return parts


def _option_raw_blocks(text: str) -> dict[str, str]:
    """Each long option's raw help blocks joined, for the CLI dump."""

    return {name: "".join(chunks) for name, chunks in _option_raw_chunks(text).items()}


def _option_blocks(text: str) -> dict[str, str]:
    """Each long option's help block joined onto one line."""

    return {opt: " ".join(raw.split()) for opt, raw in _option_raw_blocks(text).items()}


def _fold_char(ch: str) -> str:
    """Map one apostrophe or quote variant to ASCII. Other characters stay."""

    if ch in "'\"":
        return ch
    name = unicodedata.name(ch, "")
    if "QUOTATION MARK" in name or "APOSTROPHE" in name or "PRIME" in name:
        return '"' if "DOUBLE" in name else "'"
    if name in {"GRAVE ACCENT", "ACUTE ACCENT",
                "MODIFIER LETTER GRAVE ACCENT", "MODIFIER LETTER ACUTE ACCENT"}:
        return "'"
    return ch


def _normalize_text(text: str) -> str:
    """NFKC, ASCII quotes, lower case, and one space where help wrapped a line."""

    text = unicodedata.normalize("NFKC", text)
    text = "".join(_fold_char(ch) for ch in text)
    text = text.lower()
    return re.sub(r"\s+", " ", text).strip()


def _chunk_description(raw: str) -> str:
    """The option's description, without the flag header, normalized."""

    lines = raw.split("\n")
    first = lines[0].lstrip(" \t")
    gap = _FLAG_GAP.search(first)
    first_desc = first[gap.end():] if gap else ""
    rest = "\n".join(lines[1:])
    body = first_desc + ("\n" + rest if rest else "")
    return _normalize_text(body)


def _sentence_key(part: str) -> str:
    """One sentence, quotes stripped and ``default:true`` spaced like ``default: true``."""

    s = part.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    s = re.sub(r"\s*:\s*", ": ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s.strip(".").strip()


def _sentences(text: str) -> list[str]:
    return [key for part in re.split(r"[.;!?]+", text) if (key := _sentence_key(part))]


def _printable_ascii_block(raw: str) -> bool:
    """True when NFKC and quote folding leave only printable ASCII in one help block.

    ASCII whitespace stays, because wrapped help lines are collapsed later. Any other
    character that survives that fold, including ZWSP, ZWNJ, a soft hyphen, a
    confusable letter, or a combining mark, means the block cannot prove a default.
    """

    text = unicodedata.normalize("NFKC", raw)
    text = "".join(_fold_char(ch) for ch in text)
    return all(ch in _PRINTABLE_ASCII or ch in _ASCII_SPACE for ch in text)


def _unique_chunks(chunks: dict[str, list[str]], names: tuple[str, ...]) -> list[str]:
    """Raw blocks for ``names``, in order, without repeating a shared alias block."""

    seen: list[str] = []
    for name in names:
        for raw in chunks.get(name, ()):
            if raw not in seen:
                seen.append(raw)
    return seen


def _joined_description(raws: list[str]) -> str:
    return " ".join(part for raw in raws if (part := _chunk_description(raw)))


def _sentence_kind(sentence: str, *, scope: str) -> str:
    """``present``, ``absent``, ``neutral``, or ``unknown`` for one sentence.

    ``scope`` is ``bare`` (the ``--bare`` and ``--no-bare`` text together) or
    ``print``. A present allowlist sentence on ``--print`` counts only when it
    names bare. ``it'll be the default`` stays an absent signal.
    """

    if sentence in _NEUTRAL_SENTENCES:
        return "neutral"
    if sentence in _PRESENT_SENTENCES:
        if scope == "print" and _NAMES_BARE.search(sentence) is None:
            return "unknown"
        return "present"
    future = _FUTURE_DEFAULT.search(sentence) is not None
    off = scope == "bare" and _DEFAULT_OFF.search(sentence) is not None
    presentish = _PRESENTISH.search(sentence) is not None
    if (future and (presentish or off)) or (off and presentish):
        return "unknown"
    if future or off:
        return "absent"
    return "unknown"


def classify_help(text: object) -> str:
    """Return ``"absent"``, ``"present"`` or ``"unknown"`` for one build's ``claude --help`` text.

    Help is normalized first (NFKC, apostrophes and quotes folded to ASCII, wrapped
    lines collapsed, lower case). After that fold, a character outside printable
    ASCII in a ``--bare``, ``--no-bare``, or ``--print`` block is ``unknown``.

    ``present`` is proved from the whole of that text. Every ``--bare`` chunk and
    any ``--no-bare`` block are one description. Every sentence there, and every
    sentence of ``--print``, must be an allowlisted present sentence or a neutral
    fragment (``minimal mode``, ``print response and exit``), and at least one
    sentence must be present. ``--print`` contributes ``present`` only when the
    sentence names bare (``defaults to --bare``). Any other sentence is ``unknown``.
    An absent signal beside a present signal, in either option, is ``unknown``.

    ``absent`` is the explicit future-default wording (``it'll be the default`` and
    ``will be the default``, including after a folded apostrophe) or ``default:
    false`` on ``--bare``, when nothing else contradicts it.

    Owner decision (pr336 deep6): a ``--no-bare`` header is not ``present`` by
    itself. The description under that header is read with the same rule. The
    flag is still reported as ``bare_opt_out``.
    """

    if not isinstance(text, str) or not text.strip():
        return "unknown"
    chunks = _option_raw_chunks(text)
    if "--bare" not in chunks or "--print" not in chunks:
        return "unknown"
    relevant = _unique_chunks(chunks, ("--bare", "--no-bare", "--print"))
    if any(not _printable_ascii_block(raw) for raw in relevant):
        return "unknown"
    bare_sentences = _sentences(_joined_description(_unique_chunks(chunks, ("--bare", "--no-bare"))))
    print_sentences = _sentences(_joined_description(_unique_chunks(chunks, ("--print",))))
    saw_present = False
    saw_absent = False
    for scope, sentences in (("bare", bare_sentences), ("print", print_sentences)):
        for sentence in sentences:
            kind = _sentence_kind(sentence, scope=scope)
            if kind == "unknown":
                return "unknown"
            if kind == "present":
                saw_present = True
            elif kind == "absent":
                saw_absent = True
    if saw_present and saw_absent:
        return "unknown"
    if saw_present:
        return "present"
    return "absent" if saw_absent else "unknown"


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
