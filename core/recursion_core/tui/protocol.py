"""What the TUI may ask the control API, and how it reads the answers (CSL-799, candidate).

The TUI has exactly the item 8 CLI's commands (``list``, ``status RUN``, ``stop RUN``,
``start --tenant --engine --prompt [--client-key]``) and turns each into the same
``(method, params)`` the CLI sends. There is no approve, deny, order or mid-run verb: the control API
has none, and the TUI never gets a command the CLI lacks.

``conditions`` turns an API answer into text labels, so refusal, uncertainty, stale and unreconciled
states are readable without colour (Cockpit O7 ``o7.operator.v0`` presentation rules).
"""

from __future__ import annotations

import argparse
import shlex
from collections.abc import Mapping

COMMANDS = ("list", "status", "stop", "start")
METHODS = {"list": "run.list", "status": "run.status", "stop": "run.stop", "start": "run.start"}
ALLOWED_METHODS = frozenset(METHODS.values())
SUCCESS = frozenset({"ok", "started", "stopped"})
UNCERTAIN_CODES = frozenset({"pending"})
UNAVAILABLE = "control_api_unavailable"
# O7's response `surface` allows only gui or cli; the TUI labels itself cli until O7's own version step
# adds tui (PRODUCT.md 550cca0e A36). The control API takes no surface field, so this is a label only.
SURFACE = "cli"

REFUSED, UNCERTAIN, STALE, UNRECONCILED, UNAVAILABLE_LABEL = (
    "REFUSED", "UNCERTAIN", "STALE", "UNRECONCILED", "UNAVAILABLE")
LABELS = (UNAVAILABLE_LABEL, UNRECONCILED, STALE, REFUSED, UNCERTAIN)


class CommandError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        raise CommandError(message)

    def exit(self, status: int = 0, message: str | None = None):
        raise CommandError(message or "help is not a command")


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="", add_help=False)
    sub = p.add_subparsers(dest="command", required=True, parser_class=_Parser)
    sub.add_parser("list", add_help=False)
    sub.add_parser("status", add_help=False).add_argument("run_id")
    sub.add_parser("stop", add_help=False).add_argument("run_id")
    start = sub.add_parser("start", add_help=False)
    start.add_argument("--tenant", required=True)
    start.add_argument("--engine", required=True)
    start.add_argument("--prompt", required=True)
    start.add_argument("--client-key")
    return p


def parse_command(line: str) -> tuple[str, dict]:
    """``"stop run-1"`` -> ``("run.stop", {"run_id": "run-1"})``, the CLI's grammar and mapping."""

    try:
        argv = shlex.split(line)
    except ValueError as exc:
        raise CommandError(str(exc)) from None
    if not argv or argv[0] not in COMMANDS:  # own wording: argparse's differs across Python versions
        raise CommandError(f"unknown command {argv[0] if argv else ''!r}; commands are {', '.join(COMMANDS)}")
    ns = _parser().parse_args(argv)
    if ns.command == "list":
        return METHODS["list"], {}
    if ns.command in ("status", "stop"):
        return METHODS[ns.command], {"run_id": ns.run_id}
    params = {"tenant": ns.tenant, "engine": ns.engine, "prompt": ns.prompt}
    if ns.client_key:
        params["client_key"] = ns.client_key
    return METHODS["start"], params


def _runs(answer: Mapping) -> list[Mapping]:
    runs = answer.get("runs")
    if isinstance(runs, list):
        return [r for r in runs if isinstance(r, Mapping)]
    run = answer.get("run")
    return [run] if isinstance(run, Mapping) else []


def answer_conditions(answer: Mapping) -> list[tuple[str, str]]:
    """Conditions one API answer exposes, as (label, plain text). Nothing the answer says is dropped."""

    code = answer.get("code")
    found: list[tuple[str, str]] = []
    if code == UNAVAILABLE:
        found.append((UNAVAILABLE_LABEL, f"control API unavailable: {answer.get('detail', 'no answer')}"))
    elif code in UNCERTAIN_CODES:
        found.append((UNCERTAIN, f"stop not confirmed for run={answer.get('run_id')}: "
                                 f"reason={answer.get('reason', 'unknown')}; run may still be running"))
    elif code not in SUCCESS:
        what = answer.get("detail", answer.get("run_id", ""))
        found.append((REFUSED, f"{code} {what}".strip()))
    if answer.get("stale") is True:
        found.append((STALE, "API marked this answer stale"))
    for run in _runs(answer):
        if run.get("state") == "stop_pending":
            reason = (run.get("stop") or {}).get("reason") if isinstance(run.get("stop"), Mapping) else None
            found.append((UNRECONCILED, f"run={run.get('run_id')} state=stop_pending reason={reason}: "
                                        "stop requested, cleanup not confirmed"))
    if answer.get("reconciliation") == "unreconciled":
        found.append((UNRECONCILED, "API reports unreconciled state"))
    return found
