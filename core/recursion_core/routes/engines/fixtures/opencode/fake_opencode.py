"""A stand-in for ``opencode run --format json`` that replays one SYNTHETIC fixture (lane ENGINE-E3).

It is not OpenCode and runs no model. It takes the argv ``routes/engines/opencode.py`` builds (after argv[0]):

    run --format json --model <provider/model>

reads the prompt from standard input as OpenCode 1.19.5 does when its input is piped (run.ts:417-418), prints
the fixture named by ``RECURSION_FAKE_OPENCODE_FIXTURE`` (a file listed in ``index.json`` beside this script)
byte for byte, and exits with that fixture's ``exit_code``. It refuses, with nothing on stdout:

    exit 2  an argv the driver would not build, or a fixture not in index.json
    exit 3  a child environment without OPENCODE_DISABLE_AUTOUPDATE=1 (the updater must be off)
    exit 1  an empty prompt (OpenCode: "You must provide a message or a command", run.ts:421-423)

It imports nothing from Recursion, so it runs under ``python -I``.
"""

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main(argv: list[str]) -> int:
    if len(argv) != 5 or argv[:4] != ["run", "--format", "json", "--model"] or argv[4].startswith("-") \
            or "/" not in argv[4][1:]:
        sys.stderr.write("fake opencode: unexpected argv\n")
        return 2
    if os.environ.get("OPENCODE_DISABLE_AUTOUPDATE") != "1":
        sys.stderr.write("fake opencode: the updater is not off\n")
        return 3
    entries = {entry["file"]: entry for entry in json.loads((HERE / "index.json").read_text("utf-8"))["fixtures"]}
    entry = entries.get(os.environ.get("RECURSION_FAKE_OPENCODE_FIXTURE", ""))
    if entry is None:
        sys.stderr.write("fake opencode: no such fixture\n")
        return 2
    if not sys.stdin.buffer.read().strip():
        sys.stderr.write("You must provide a message or a command\n")
        return 1
    sys.stdout.buffer.write((HERE / entry["file"]).read_bytes())
    sys.stdout.buffer.flush()
    return entry["exit_code"]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
