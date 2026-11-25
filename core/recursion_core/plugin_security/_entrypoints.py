"""Command line for ``recursion plugin``.

The program name is ``recursion``. A refusal prints ``refused: CODE`` on stderr
and returns 1; a refusal that says how to end it prints that sentence on the
next line. A command that completes after keeping a plugin's extra entries aside
prints one line on stderr for each plugin, saying which and where. A read or
write the system refuses (a full or read-only disk, no permission, a path that
is not a folder) prints ``refused: filesystem_refused``
and the system's message, with no traceback, and returns 1. A usage error
returns 2. ``install --git`` reads one local bare repository. The entry URL
is not fetched. Nothing from the fetched tree is executed. ``enable``,
``disable`` and ``rollback`` are one command each.

``unpin <id>``, and an install that would move a pin to another trusted key,
ask one question on stderr and read one line: only the typed word ``yes`` goes
on. Any other answer, or none, prints ``not changed:`` and why on stderr,
writes nothing and returns 1.

An install of a signed release of a pinned plugin that asks for more is held,
not installed: it prints one line on stderr for each thing it asks for more,
then one line with the commands that go on, and returns 0.
``quarantine list`` shows what is held; ``quarantine accept <id>`` asks one
question, as unpin does, and installs the held release only on ``yes``;
``quarantine drop <id>`` moves the held release into a kept place and names it.

``install --yes`` writes one grant for the requested permissions except
``order``. ``install --no-grant`` writes none. With neither flag, a terminal
is asked once and only the answer ``y`` or ``yes`` grants; any other answer,
and a run that is not a terminal, writes no grant.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from pathlib import Path

from .install import (InstallRefused, NothingChanged, disable_plugin, enable_plugin, install_plugin,
                      list_plugins, quarantine_accept, quarantine_drop, quarantine_list, remove_plugin,
                      rollback_generation, unpin_plugin)

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
    install.add_argument("--git", help="local bare repository")
    install.add_argument("--config")
    install.add_argument("--state")
    install.add_argument("--yes", action="store_true",
                         help="write one grant for the requested permissions except order")
    install.add_argument("--no-grant", action="store_true", help="write no grant")

    listed = commands.add_parser("list")
    listed.error = _error  # type: ignore[method-assign]
    listed.add_argument("--config")
    listed.add_argument("--state")

    remove = commands.add_parser("remove")
    remove.error = _error  # type: ignore[method-assign]
    remove.add_argument("--plugin", required=True)
    remove.add_argument("--config")
    remove.add_argument("--state")

    enable = commands.add_parser("enable")
    enable.error = _error  # type: ignore[method-assign]
    enable.add_argument("--plugin", required=True)
    enable.add_argument("--config")
    enable.add_argument("--state")

    disable = commands.add_parser("disable")
    disable.error = _error  # type: ignore[method-assign]
    disable.add_argument("--plugin", required=True)
    disable.add_argument("--config")
    disable.add_argument("--state")

    rollback = commands.add_parser("rollback")
    rollback.error = _error  # type: ignore[method-assign]
    rollback.add_argument("--config")
    rollback.add_argument("--state")

    unpin = commands.add_parser("unpin")
    unpin.error = _error  # type: ignore[method-assign]
    unpin.add_argument("plugin_id")
    unpin.add_argument("--config")
    unpin.add_argument("--state")

    quarantine = commands.add_parser("quarantine")
    quarantine.error = _error  # type: ignore[method-assign]
    actions = quarantine.add_subparsers(dest="action", required=True)
    for action in ("list", "accept", "drop"):
        held = actions.add_parser(action)
        held.error = _error  # type: ignore[method-assign]
        if action != "list":
            held.add_argument("plugin_id")
        held.add_argument("--config")
        held.add_argument("--state")
    return parser


def _confirm_grant(stdin, err):
    """Ask on a terminal. Only ``y`` or ``yes`` returns True. Anything else is no."""

    def confirm(requested, checks):
        if not getattr(stdin, "isatty", lambda: False)():
            return False
        err.write("Requested permissions: %s\n" % ", ".join(requested))
        for line in checks:
            err.write("%s\n" % line)
        err.write("Grant the requested permissions except order? [y/N] ")
        err.flush()
        line = stdin.readline()
        if type(line) is not str:
            return False
        return line.strip().lower() in {"y", "yes"}

    return confirm


def main(argv: list[str] | None = None, out=None, err=None, inp=None, stdin=None) -> int:
    """Run one plugin command. Returns 0, 1 (refused, or a yes not given) or 2 (usage).

    ``inp`` and ``stdin`` name the one input stream; ``inp`` is used when both are given.
    """
    # A caller that supplies streams is not the operator's terminal. Do not
    # read the process stdin for the grant question unless that caller passes
    # one: a captured run must not block, and it must not take a silent yes.
    given = inp if inp is not None else stdin
    embedded = out is not None or err is not None or given is not None
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    # A typed-yes question (a pin, a held release) reads the given stream, else the process stdin.
    inp = sys.stdin if given is None else given
    # The grant question reads the given stream; with none, it reads stdin only for a caller
    # that supplied no stream.
    stdin = given if given is not None else (io.StringIO("") if embedded else sys.stdin)
    if argv is None:
        argv = sys.argv[1:]
    try:
        args = _parser().parse_args(argv)
    except _Usage as exc:
        err.write(f"usage: recursion plugin install|list|remove|enable|disable|rollback|unpin|quarantine ({exc})\n")
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
            if args.yes and args.no_grant:
                err.write("usage: recursion plugin install (--yes and --no-grant)\n")
                return 2
            if args.no_grant:
                write_grant, confirm = False, None
            elif args.yes:
                write_grant, confirm = True, None
            else:
                write_grant, confirm = None, _confirm_grant(stdin, err)
            result = install_plugin(_config_dir(args.config), args.registry, args.plugin,
                                    version=args.version, state=_state_dir(args.state), report_kept=report,
                                    confirm=ask, git_repository=args.git, write_grant=write_grant,
                                    confirm_grant=confirm)
        elif args.command == "list":
            result = list_plugins(_config_dir(args.config), state=_state_dir(args.state))
        elif args.command == "enable":
            result = enable_plugin(_config_dir(args.config), args.plugin, state=_state_dir(args.state),
                                   report_kept=report)
        elif args.command == "disable":
            result = disable_plugin(_config_dir(args.config), args.plugin, state=_state_dir(args.state),
                                    report_kept=report)
        elif args.command == "rollback":
            result = rollback_generation(_config_dir(args.config), state=_state_dir(args.state))
        elif args.command == "unpin":
            result = unpin_plugin(_config_dir(args.config), args.plugin_id, state=_state_dir(args.state),
                                  report_kept=report, confirm=ask)
        elif args.command == "quarantine" and args.action == "list":
            result = quarantine_list(_config_dir(args.config), state=_state_dir(args.state))
        elif args.command == "quarantine" and args.action == "accept":
            result = quarantine_accept(_config_dir(args.config), args.plugin_id, state=_state_dir(args.state),
                                       report_kept=report, confirm=ask)
        elif args.command == "quarantine":
            result = quarantine_drop(_config_dir(args.config), args.plugin_id, state=_state_dir(args.state))
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
    if result.get("result") == "quarantined":
        # What the held release asks for more, one line each, then the commands that go on from there.
        for line in [*result["asks"], result["next"]]:
            err.write(f"{line}\n")
    out.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0
