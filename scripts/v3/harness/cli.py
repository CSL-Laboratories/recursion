"""``python -m scripts.v3.harness`` — doctor, init, run, status, logs, land, and clean."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scripts.v3.harness.clean import clean_runs
from scripts.v3.harness.config import default_enabled, load_config, write_starter
from scripts.v3.harness.doctor import diagnose
from scripts.v3.harness.errors import HarnessError
from scripts.v3.harness.land import land_run
from scripts.v3.harness.log import read_log
from scripts.v3.harness.run import run_task
from scripts.v3.harness.state import list_runs, load_run


_EXIT = {"completed": 0, "unreviewed": 2, "review-rejected": 2, "budget-exceeded": 3}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.v3.harness",
        description="Route one task through a provider adapter.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="check git and each configured provider")
    doctor.add_argument("--config", type=Path, help="config JSON (default: config/harness.json)")

    init = commands.add_parser("init", help="write a starter config")
    init.add_argument("path", type=Path, help="config path to create")
    init.add_argument("--force", action="store_true", help="overwrite an existing file")

    run = commands.add_parser("run", help="route a task and review it when a second provider is enabled")
    run.add_argument("--task", required=True, help="task text")
    run.add_argument("--config", type=Path, help="config JSON (default: config/harness.json)")
    run.add_argument("--state", type=Path, help="directory for the saved run")
    run.add_argument("--provider", help="worker provider; other capable providers can still review")
    run.add_argument("--branch", help="branch land merges after the run is accepted")
    run.add_argument("--allow-live", action="store_true", help="start a headless CLI; tests and CI omit this")

    status = commands.add_parser("status", help="show saved runs")
    status.add_argument("--state", type=Path, help="directory for saved runs")
    status.add_argument("--run", help="print one saved run")

    logs = commands.add_parser("logs", help="print one run's events")
    logs.add_argument("run_id", help="saved run id")
    logs.add_argument("--state", type=Path, help="directory for saved runs")

    land = commands.add_parser("land", help="merge an accepted run branch")
    land.add_argument("run_id", help="saved run id")
    land.add_argument("--state", type=Path, help="directory for saved runs")
    land.add_argument("--repo", type=Path, help="git repository (default: the current directory)")
    land.add_argument("--into", help="checked-out branch that receives the merge")

    clean = commands.add_parser("clean", help="remove finished run state")
    clean.add_argument("--state", type=Path, help="directory for saved runs")
    clean.add_argument("--run", help="one run to remove; omit to remove every finished run")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            report = diagnose(load_config(args.config))
            _emit(report)
            return 0 if report["ok"] else 1
        if args.command == "init":
            enabled = default_enabled()
            write_starter(args.path, enabled, force=args.force)
            _emit({"enabled": sorted(enabled), "path": str(args.path)})
            return 0
        if args.command == "run":
            record = run_task(
                args.task,
                config=load_config(args.config),
                state_dir=_state_dir(args.state),
                provider=args.provider,
                allow_live=args.allow_live,
                run_branch=args.branch,
            )
            _emit(record)
            return _EXIT.get(record["status"], 1)
        if args.command == "status":
            state_dir = _state_dir(args.state)
            if args.run:
                _emit(load_run(state_dir, args.run))
            else:
                _emit({"runs": list_runs(state_dir)})
            return 0
        if args.command == "logs":
            _emit({"events": read_log(_state_dir(args.state), args.run_id)})
            return 0
        if args.command == "land":
            repo = Path.cwd() if args.repo is None else args.repo
            _emit(land_run(_state_dir(args.state), args.run_id, repo=repo, into=args.into))
            return 0
        if args.command == "clean":
            _emit(clean_runs(_state_dir(args.state), args.run))
            return 0
    except HarnessError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    raise HarnessError(f"unknown command {args.command}")


def _state_dir(path: Path | None) -> Path:
    if path is not None:
        return path
    return Path.cwd() / ".recursion" / "runs"


def _emit(payload: dict) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
