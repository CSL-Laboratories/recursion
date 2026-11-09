"""Read build help for print-mode bare defaults and a documented auth-mode opt-out.

Unknown or incomplete help fails closed. Only help is executed; no provider call,
settings edit or credential lookup is performed. Adapters consume the reported
metadata rather than probing the executable themselves.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping

STATES = ("absent", "present", "unknown")
HELP_TIMEOUT_S = 10.0
HELP_MAX_BYTES = 1 << 20

# An option line: two leading spaces, then the flag (optionally after a short flag).
_OPTION = re.compile(r"^[ \t]{1,8}(?:-\w,[ \t]*)?(--[a-z0-9][a-z0-9-]*)", re.MULTILINE)
# Present-tense "default for -p" wording in the --bare block. Future-tense notes are filtered first.
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
_FUTURE = re.compile(r"\bwill\s+become\b|\bfuture\s+release\b|\bin\s+a\s+future\b|\bplanned\b|\bupcoming\b",
                     re.IGNORECASE)
# Negated or conditional wording never proves the default either way.
_NEGATED = re.compile(r"\bnot\b|\bno\s+longer\b|\bnever\b|\bunless\b|\bexcept\b|\bwithout\b|n't\b",
                      re.IGNORECASE)
# An option that turns bare mode off would only exist if bare can be on without the flag.
_OPT_OUT = ("--no-bare",)


def _option_raw_blocks(text: str) -> dict[str, str]:
    """Map each long option to its raw help lines (the option line plus its wrapped description)."""

    blocks: dict[str, str] = {}
    matches = list(_OPTION.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        blocks.setdefault(m.group(1), text[m.start():end])
    return blocks


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
    for option in ("--bare", "--print"):
        for sentence in _sentences(blocks[option]):
            if _FUTURE.search(sentence):
                if "bare" in sentence.lower() and _DEFAULT_NOW.search(sentence):
                    absent = True
                continue
            # Either block saying --bare is the default now is enough: print's own help may announce
            # it even when the --bare line does not.
            patterns = (_PRINT_BARE_DEFAULT,) if option == "--print" else (_DEFAULT_NOW, _BARE_SELF_DEFAULT)
            if any(_affirmed(p, sentence) for p in patterns):
                return "present"
            if option == "--bare" and re.search(r"default\s*[:=]?\s*(?:false|off|disabled)\b",
                                                sentence, re.I):
                absent = True
    return "absent" if absent else "unknown"


def auth_mode(text: object) -> dict:
    """Route metadata, including only an opt-out actually advertised by this build."""
    state = classify_help(text)
    blocks = _option_blocks(text) if isinstance(text, str) else {}
    return {"bare_default": state,
            "bare_opt_out": next((flag for flag in _OPT_OUT if flag in blocks), None)}


Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


def read_help(binary: str, parent_env: Mapping[str, str], run: Runner = subprocess.run) -> str | None:
    """Run ``<binary> --help`` once and return its text, or ``None`` if it cannot be read.

    The child gets only ``PATH``, ``HOME`` and ``LANG`` from the explicit ``parent_env`` (never
    ``os.environ``), so no credential variable reaches it. A non-zero exit, timeout, launch failure or
    non-UTF-8 output is ``None`` (classified ``"unknown"``).
    """

    env = {k: parent_env[k] for k in ("PATH", "HOME", "LANG") if isinstance(parent_env.get(k), str)}
    try:
        done = run([binary, "--help"], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
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


def probe(binary: str, parent_env: Mapping[str, str], run: Runner = subprocess.run) -> str:
    """``classify_help(read_help(...))``: the value a surface reports as ``route.bare_default``."""

    return classify_help(read_help(binary, parent_env, run))


def probe_auth_mode(binary: str, parent_env: Mapping[str, str], run: Runner = subprocess.run) -> dict:
    """Read help once and report the explicit subscription auth-mode capability."""
    return auth_mode(read_help(binary, parent_env, run))


def main(argv: list[str] | None = None, run: Runner = subprocess.run) -> int:
    """Print one build's classification and the help lines it came from. Runs only ``--help``."""

    parser = argparse.ArgumentParser(prog="python -m recursion_core.routes.bare_default",
                                     description="Classify whether --bare is the -p default for one claude build.")
    parser.add_argument("--claude", required=True, help="path to the claude binary; only --help is run")
    args = parser.parse_args(argv)
    text = read_help(args.claude, os.environ, run)
    metadata = auth_mode(text)
    # The adapter owns the refusal rule; report its answer rather than restating it here.
    from recursion_core.routes import contract as c
    from recursion_core.routes.claude_code import ClaudeCodeAdapter
    try:
        flags = ClaudeCodeAdapter._subscription_auth_args(dict(metadata))
        route = "launches" + (" with " + " ".join(flags) if flags else "")
    except c.RouteError as exc:
        route = f"refuses ({exc})"
    print(f"binary: {args.claude}")
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
