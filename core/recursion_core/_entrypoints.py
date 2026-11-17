"""The ``recursiond`` console script (distribution ``csl-recursion``). Standard library only.

    recursiond install | upgrade | uninstall    the per-user systemd unit (control/commands/setup.py)
    recursiond serve [daemon options]           run the daemon in the foreground
    recursiond observe                          local tool setup, owner-only record

Linux only: on any other system every command refuses with ``platform_unsupported`` (exit 1)
before anything else is imported; ``--help`` still prints. Output is one JSON object per command;
0 is ok, 1 a refusal, 2 a usage error.

``serve`` checks the build before it imports anything of the daemon's: the daemon needs
free-threaded CPython 3.14 (``build_not_free_threaded`` otherwise). It then replaces this process
with ``<this interpreter> -X gil=0 -m recursion_core.daemon <options>``, so the GIL is off from the
interpreter's first line, whatever ``PYTHON_GIL`` says, and the daemon module runs as a module
(review check.md, row C047). It re-executes into the daemon module, never into this one, so it
cannot loop; the daemon checks the GIL again itself. The installed unit does not go through
``serve``: it starts the daemon module directly (``daemon_install.unit.daemon_exec_args``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import sysconfig

PROG = "recursiond"
DAEMON_MODULE = "recursion_core.daemon"
EXIT_OK, EXIT_REFUSED, EXIT_USAGE = 0, 1, 2
# The doctor-command draft adds this table too. It is not on the doctor-core base, so this
# draft adds the minimal table. The two drafts conflict on this name. This row is observe.
SUBCOMMANDS = (
    ("observe", "report local tool setup into an owner-only record; secret values stay out"),
)
_DESCRIPTION = ("Recursion's per-user daemon and its installer. Linux only. "
                "Each command prints one JSON object; \"code\": \"ok\" is success.")


class _Usage(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # a usage error is an exit status, as in recursion-control
        raise _Usage(message)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["serve"]:
        return serve(argv[1:])
    if argv[:1] and argv[0] not in ("-h", "--help") and not _linux():
        return _refuse("platform_unsupported", _platform_detail())
    from recursion_core.control.commands import setup

    parser = _Parser(prog=PROG, description=_DESCRIPTION)
    commands = parser.add_subparsers(dest="command", metavar="command")
    setup.parser(commands)
    for name, help_text in SUBCOMMANDS:
        commands.add_parser(name, help=help_text)
    commands.add_parser("serve", help="run the daemon in the foreground; options after it go to the daemon",
                        add_help=False)
    try:
        args = parser.parse_args(argv)
    except _Usage as exc:
        print(f"usage error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except SystemExit as exc:  # --help
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    if args.command is None:
        parser.print_usage(sys.stderr)
        print("usage error: a command is required", file=sys.stderr)
        return EXIT_USAGE
    args.out, args.err = sys.stdout, sys.stderr
    if args.command in {name for name, _help in SUBCOMMANDS}:
        return _dispatch_subcommand(args)
    return setup.dispatch(args, None)


def _dispatch_subcommand(args) -> int:
    import importlib

    if args.command == "observe":
        module = importlib.import_module("recursion_core.setup.observe")
        return module.command(out=args.out, err=args.err)
    print(f"usage error: {args.command}", file=sys.stderr)
    return EXIT_USAGE


def serve(args) -> int:
    """Run the daemon in this process's place, with the GIL off. Returns only on a refusal."""

    if not _linux():
        return _refuse("platform_unsupported", _platform_detail())
    if not _daemon_build():
        return _refuse("build_not_free_threaded",
                       f"the daemon needs free-threaded CPython 3.14 (python3.14t); this is "
                       f"{sys.implementation.name} {sys.version_info[0]}.{sys.version_info[1]}"
                       f"{' free-threaded' if sysconfig.get_config_var('Py_GIL_DISABLED') else ' with the GIL'}")
    if not sys.executable or not os.path.isabs(sys.executable):
        return _refuse("interpreter_unknown", "this interpreter does not know its own path")
    try:
        os.execv(sys.executable, daemon_argv(sys.executable, args))
    except OSError as exc:  # execv returns only by raising
        return _refuse("interpreter_unknown", f"{sys.executable} could not be started: {type(exc).__name__}: {exc}")
    return EXIT_REFUSED  # not reached: execv either replaces the process or raises


def daemon_argv(interpreter: str, args) -> list[str]:
    return [interpreter, "-X", "gil=0", "-m", DAEMON_MODULE, *args]


def _linux() -> bool:
    return sys.platform.startswith("linux")


def _daemon_build() -> bool:
    return (sys.implementation.name == "cpython" and sys.version_info[:2] == (3, 14)
            and bool(sysconfig.get_config_var("Py_GIL_DISABLED")))


def _platform_detail() -> str:
    return (f"recursiond runs on Linux (a systemd user service); this system is {sys.platform}. "
            "Windows and macOS are not supported yet.")


def _refuse(code: str, detail: str) -> int:
    print(json.dumps({"code": code, "detail": detail}, sort_keys=True), file=sys.stdout)
    return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
