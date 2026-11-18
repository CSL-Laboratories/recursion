"""Command line for ``recursion plugin``.

The program name is ``recursion``. A refusal prints ``refused: CODE`` on stderr
and returns 1. A usage error returns 2. ``install --git`` reads one local bare
repository. The entry URL is not fetched. Nothing from the fetched tree is
executed. ``enable``, ``disable`` and ``rollback`` are one command each.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .install import (
    InstallRefused, disable_plugin, enable_plugin, install_plugin, list_plugins, remove_plugin,
    rollback_generation,
)

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
        err.write(f"usage: recursion plugin install|list|remove|enable|disable|rollback ({exc})\n")
        return 2
    try:
        if args.command == "install":
            result = install_plugin(_config_dir(args.config), args.registry, args.plugin,
                                    version=args.version, state=_state_dir(args.state),
                                    git_repository=args.git)
        elif args.command == "list":
            result = list_plugins(_config_dir(args.config), state=_state_dir(args.state))
        elif args.command == "remove":
            result = remove_plugin(_config_dir(args.config), args.plugin, state=_state_dir(args.state))
        elif args.command == "enable":
            result = enable_plugin(_config_dir(args.config), args.plugin, state=_state_dir(args.state))
        elif args.command == "disable":
            result = disable_plugin(_config_dir(args.config), args.plugin, state=_state_dir(args.state))
        else:
            result = rollback_generation(_config_dir(args.config), state=_state_dir(args.state))
    except InstallRefused as exc:
        for code in exc.codes:
            err.write(f"refused: {code}\n")
        return 1
    out.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0
