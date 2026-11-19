"""Command line for ``recursion plugin install``, ``list``, ``remove`` and ``unpin``.

The program name is ``recursion``. A refusal prints ``refused: CODE`` on stderr
and returns 1; a refusal that says how to end it prints that sentence on the
next line. A command that completes after keeping a plugin's extra entries aside
prints one line on stderr for each plugin, saying which and where. A read or
write the system refuses (a full or read-only disk, no permission, a path that
is not a folder) prints ``refused: filesystem_refused``
and the system's message, with no traceback, and returns 1. A usage error
returns 2. Nothing here fetches.

``unpin <id>``, and an install that would move a pin to another trusted key,
ask one question on stderr and read one line: only the typed word ``yes`` goes
on. Any other answer, or none, prints ``not changed:`` and why on stderr,
writes nothing and returns 1.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .install import InstallRefused, NothingChanged, install_plugin, list_plugins, remove_plugin, unpin_plugin

__all__ = ["main"]


class _Usage(Exception):
    pass


def _error(message: str) -> None:
    raise _Usage(message)


def _config_dir(value: str | None) -> Path:
    if value:
        return Path(value)
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "recursion"


def _state_dir(value: str | None) -> Path:
    if value:
        return Path(value)
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "recursion"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="recursion")
    parser.error = _error  # type: ignore[method-assign]
    group = parser.add_subparsers(dest="group", required=True)
    plugin = group.add_parser("plugin")
    plugin.error = _error  # type: ignore[method-assign]
    commands = plugin.add_subparsers(dest="command", required=True)

    install = commands.add_parser("install")
    install.error = _error  # type: ignore[method-assign]
    install.add_argument("--registry", required=True)
    install.add_argument("--plugin", required=True)
    install.add_argument("--version")
    install.add_argument("--config")
    install.add_argument("--state")

    listed = commands.add_parser("list")
    listed.error = _error  # type: ignore[method-assign]
    listed.add_argument("--config")
    listed.add_argument("--state")

    remove = commands.add_parser("remove")
    remove.error = _error  # type: ignore[method-assign]
    remove.add_argument("--plugin", required=True)
    remove.add_argument("--config")
    remove.add_argument("--state")

    unpin = commands.add_parser("unpin")
    unpin.error = _error  # type: ignore[method-assign]
    unpin.add_argument("plugin_id")
    unpin.add_argument("--config")
    unpin.add_argument("--state")
    return parser


def main(argv: list[str] | None = None, out=None, err=None, inp=None) -> int:
    """Run one plugin command. Returns 0, 1 (refused, or a yes not given) or 2 (usage)."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    inp = sys.stdin if inp is None else inp
    if argv is None:
        argv = sys.argv[1:]
    try:
        args = _parser().parse_args(argv)
    except _Usage as exc:
        err.write(f"usage: recursion plugin install|list|remove ({exc})\n")
        return 2
    said = []

    def report(line):
        # A kept place is named the moment it is made, before any later step can fail.
        err.write(f"{line}\n")
        flush = getattr(err, "flush", None)
        if flush is not None:
            flush()
        said.append(line)

    def ask(question):
        # One question on stderr, one typed line read back; an end of input is no answer.
        err.write(f"{question}\n")
        flush = getattr(err, "flush", None)
        if flush is not None:
            flush()
        return inp.readline()

    try:
        if args.command == "install":
            result = install_plugin(_config_dir(args.config), args.registry, args.plugin,
                                    version=args.version, state=_state_dir(args.state), report_kept=report,
                                    confirm=ask)
        elif args.command == "list":
            result = list_plugins(_config_dir(args.config), state=_state_dir(args.state))
        elif args.command == "unpin":
            result = unpin_plugin(_config_dir(args.config), args.plugin_id, state=_state_dir(args.state),
                                  report_kept=report, confirm=ask)
        else:
            result = remove_plugin(_config_dir(args.config), args.plugin, state=_state_dir(args.state),
                                   report_kept=report)
    except NothingChanged as exc:
        err.write(f"not changed: {exc.detail}\n")
        return 1
    except InstallRefused as exc:
        # A read or write the system refused is the typed refusal filesystem_refused. What was written stays
        # as after a stop of the process: the next command finishes or drops the stopped publish.
        for code in exc.codes:
            err.write(f"refused: {code}\n")
        if exc.show_detail:
            err.write(f"{exc.detail}\n")
        for line in getattr(exc, "kept", ()):
            if line not in said:
                err.write(f"{line}\n")
        return 1
    for line in result.get("kept", ()):
        if line not in said:
            err.write(f"{line}\n")
    out.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0
