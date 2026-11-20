"""A fake ``grok`` for the one-shot drive (SYNTHETIC; lane ENGINE-E4). It is not Grok and runs no model.

Run as ``python fake_grok.py <arguments>`` with ``FAKE_GROK_CASE`` naming one case file of
``oneshot/``. It accepts exactly the arguments ``routes/engines/grok.py:oneshot_argv`` makes, in any
order: ``--prompt-file <path>`` (a file that exists and is not empty), ``--output-format
streaming-json``, and at most one ``--model <model>``. Anything else, or a missing one, ends it with
exit 2 and a line on standard error, as a command-line parser does. Then it writes the case's
standard output byte for byte and exits with the case's exit code.

It imports nothing from Recursion, so it can be run with ``python -I``.
"""

from __future__ import annotations

import json
import os
import sys


def case_lines(case: dict) -> list[bytes]:
    """The case's standard output, one item per line, as bytes. The rule is the README's."""

    out = []
    for item in case["stdout"]:
        if isinstance(item, str):
            out.append(item.encode("utf-8"))
        else:
            out.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
    return out


def refuse(message: str) -> int:
    sys.stderr.write(f"fake grok: {message}\n")
    return 2


def main(argv: list[str]) -> int:
    seen: dict[str, str] = {}
    rest = list(argv)
    while rest:
        flag = rest.pop(0)
        if flag not in ("--prompt-file", "--output-format", "--model"):
            return refuse(f"unexpected argument {flag!r}")
        if flag in seen:
            return refuse(f"{flag} given twice")
        if not rest:
            return refuse(f"{flag} needs a value")
        seen[flag] = rest.pop(0)
    if seen.get("--output-format") != "streaming-json":
        return refuse("--output-format streaming-json is required")
    prompt = seen.get("--prompt-file")
    if prompt is None:
        return refuse("--prompt-file is required")
    try:
        with open(prompt, "rb") as handle:
            if not handle.read(1):
                return refuse("the prompt file is empty")
    except OSError:
        return refuse("the prompt file cannot be read")
    with open(os.environ["FAKE_GROK_CASE"], encoding="utf-8") as handle:
        case = json.load(handle)
    for line in case_lines(case):
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()
    return int(case["exit_code"])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
