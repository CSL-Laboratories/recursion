"""Command line for ``recursion plugin install``, ``list`` and ``remove``.

The program name is ``recursion``. A refusal prints ``refused: CODE`` on stderr
and returns 1. A usage error returns 2. Nothing here fetches.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .install import InstallRefused, install_plugin, list_plugins, remove_plugin

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
    return parser


def main(argv: list[str] | None = None, out=None, err=None) -> int:
    """Run one plugin command. Returns 0, 1 (refused) or 2 (usage)."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    if argv is None:
        argv = sys.argv[1:]
    try:
        args = _parser().parse_args(argv)
    except _Usage as exc:
        err.write(f"usage: recursion plugin install|list|remove ({exc})\n")
        return 2
    try:
        if args.command == "install":
            result = install_plugin(_config_dir(args.config), args.registry, args.plugin,
                                    version=args.version, state=_state_dir(args.state))
        elif args.command == "list":
            result = list_plugins(_config_dir(args.config), state=_state_dir(args.state))
        else:
            result = remove_plugin(_config_dir(args.config), args.plugin, state=_state_dir(args.state))
    except InstallRefused as exc:
        for code in exc.codes:
            err.write(f"refused: {code}\n")
        return 1
    out.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0
