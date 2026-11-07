"""Control CLI (CSL-798, candidate). See README.md.

A thin surface over the control API: it parses arguments, makes one ``api.call(method, params)``, prints
the API's answer verbatim as JSON and maps its code to an exit status. It has no process or signal
access of its own; Stop goes through the control API like every other surface.

Exit codes: 0 ok/started/stopped, 3 pending (stop not confirmed), 4 run_unknown, 1 other API error,
2 usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from typing import TextIO

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_PENDING, EXIT_UNKNOWN = 0, 1, 2, 3, 4
_EXIT = {"ok": EXIT_OK, "started": EXIT_OK, "stopped": EXIT_OK, "pending": EXIT_PENDING, "run_unknown": EXIT_UNKNOWN}


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # usage errors return a code instead of exiting the process
        raise _Usage(message)


class _Usage(Exception):
    pass


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="recursion-control")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    sub.add_parser("status").add_argument("run_id")
    sub.add_parser("stop").add_argument("run_id")
    start = sub.add_parser("start")
    start.add_argument("--tenant", required=True)
    start.add_argument("--engine", required=True)
    start.add_argument("--prompt", required=True)
    start.add_argument("--client-key")
    return p


def main(argv: list[str], api, out: TextIO | None = None, err: TextIO | None = None) -> int:
    """Run one command against ``api`` (anything with ``call(method, params) -> dict``)."""

    out, err = out or sys.stdout, err or sys.stderr
    try:
        ns = _parser().parse_args(argv)
    except _Usage as exc:
        print(f"usage error: {exc}", file=err)
        return EXIT_USAGE
    if ns.command == "list":
        method, params = "run.list", {}
    elif ns.command in ("status", "stop"):
        method, params = f"run.{ns.command}", {"run_id": ns.run_id}
    else:
        method = "run.start"
        params = {"tenant": ns.tenant, "engine": ns.engine, "prompt": ns.prompt}
        if ns.client_key:
            params["client_key"] = ns.client_key
    result = api.call(method, params)
    print(json.dumps(result, sort_keys=True), file=out)
    return _EXIT.get(result.get("code"), EXIT_ERROR)


def run(argv: list[str], api_factory: Callable[[], object] | None) -> int:
    """Entry point for a packaged command: the daemon client factory is injected by the caller."""

    if api_factory is None:
        print(json.dumps({"code": "control_api_unavailable"}), file=sys.stdout)
        return EXIT_ERROR
    return main(argv, api_factory())
