"""Is ``--bare`` the ``-p`` default in this claude build? (CSL-726, B20; candidate). See README.md.

Anthropic's headless docs say ``--bare`` "will become the default for ``-p`` in a future release", and
the 2.1.312 help says that under ``--bare`` "Anthropic auth is strictly ANTHROPIC_API_KEY or apiKeyHelper
... (OAuth and keychain are never read)". The subscription route launches ``claude -p`` without
``--bare`` and relies on the user's own sign-in. If a build makes ``--bare`` the ``-p`` default, that run
would ignore the sign-in, so the route has to know before it launches.

No opt-out flag is in repository evidence, so this module does not invent one. It reads the build's
own ``--help`` text and classifies it:

* ``"absent"``: the ``--bare`` option is listed and nothing says it is (now) the ``-p`` default.
  A future-tense note ("will become", "future release") also reads as absent.
* ``"present"``: the help says, in the present tense, that ``--bare`` is the ``-p`` / print-mode
  default (or lists an option that turns bare mode off for ``-p``).
* ``"unknown"``: no ``--bare`` option in the text, empty text, or the help could not be read.

The surface (or the R2a launch check, CSL-732) reports the result as ``route.bare_default``; the Claude
Code adapter never runs the binary itself. ``read_help`` is the one place that does, with ``--help``
only, a short timeout and a small fixed environment: no sign-in, settings or credential file is read
or written by Recursion, and no request leaves the machine on Recursion's behalf.
"""

from __future__ import annotations

import re
import subprocess
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
_FUTURE = re.compile(r"\bwill\s+become\b|\bfuture\s+release\b|\bin\s+a\s+future\b|\bplanned\b|\bupcoming\b",
                     re.IGNORECASE)
# An option that turns bare mode off would only exist if bare can be on without the flag.
_OPT_OUT = ("--no-bare",)


def _option_blocks(text: str) -> dict[str, str]:
    """Map each long option to its help block (the option line plus its wrapped description)."""

    blocks: dict[str, str] = {}
    matches = list(_OPTION.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        blocks.setdefault(m.group(1), " ".join(text[m.start():end].split()))
    return blocks


def _sentences(block: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.;])\s+", block) if s]


def classify_help(text: object) -> str:
    """Return ``"absent"``, ``"present"`` or ``"unknown"`` for one build's ``claude --help`` text."""

    if not isinstance(text, str) or not text.strip():
        return "unknown"
    blocks = _option_blocks(text)
    if "--bare" not in blocks:
        return "unknown"
    if any(opt in blocks for opt in _OPT_OUT):
        return "present"
    for sentence in _sentences(blocks["--bare"]):
        if _DEFAULT_NOW.search(sentence) and not _FUTURE.search(sentence):
            return "present"
    return "absent"


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
