#!/usr/bin/env python3
"""Measure one child host's ready time and idle memory; print three JSON rows."""
from __future__ import annotations

import argparse
import json
import math
import platform
import queue
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

READY_TIMEOUT_S = 10.0
STOP_TIMEOUT_S = 5.0


def builtin_command() -> list[str]:
    """Return the minimal isolated, idling interpreter command."""
    program = "import time\nprint('ready', flush=True)\nwhile True: time.sleep(3600)"
    return [sys.executable, "-I", "-X", "utf8", "-B", "-c", program]


def parse_status(text: str) -> tuple[int, int]:
    """Extract (VmRSS, VmHWM), converting proc's kB labels to integer KiB."""
    values = {}
    for line in text.splitlines():
        key, separator, raw = line.partition(":")
        fields = raw.split()
        if separator and key in ("VmRSS", "VmHWM") and len(fields) == 2 and fields[1] == "kB":
            try:
                values[key] = int(fields[0])
            except ValueError:
                pass
    missing = [key for key in ("VmRSS", "VmHWM") if key not in values]
    if missing:
        raise ValueError("status lacks valid kB lines: " + ", ".join(missing))
    return values["VmRSS"], values["VmHWM"]


def read_memory(pid: int, proc_root: str | Path = "/proc") -> tuple[int, int]:
    """Read resident and peak resident memory for pid beneath proc_root."""
    path = Path(proc_root) / str(pid) / "status"
    try:
        return parse_status(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read status file {path}: {exc}") from exc


def start_host(command: list[str]) -> subprocess.Popen[bytes]:
    """Start only the requested child, with a pipe for its ready line."""
    return subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )


def stop_host(process: subprocess.Popen[bytes]) -> None:
    """End this exact child through its handle, escalating only if necessary."""
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        process.wait()


def measure_once(
    command: list[str], *, idle_s: float = 2.0, proc_root: str | Path = "/proc",
    ready_timeout_s: float = READY_TIMEOUT_S,
) -> tuple[float | None, int | None, int | None, str | None]:
    """Return start milliseconds, RSS KiB, peak KiB, and an optional error."""
    started = time.monotonic()
    try:
        process = start_host(command)
    except OSError as exc:
        reason = f"cannot start child: {exc}"
        return None, None, None, reason
    ready_lines: queue.Queue = queue.Queue(maxsize=1)

    def read_ready() -> None:
        try:
            line = process.stdout.readline()
        except Exception as exc:  # surfaced as data, never as a failing measurement
            line = exc
        ready_lines.put((line, time.monotonic()))

    reader = threading.Thread(target=read_ready, daemon=True)
    reader.start()
    try:
        try:
            remaining = max(0.0, started + ready_timeout_s - time.monotonic())
            line, printed_at = ready_lines.get(timeout=remaining)
        except queue.Empty:
            return None, None, None, f"no ready line within {ready_timeout_s:g} seconds"
        if isinstance(line, Exception):
            return None, None, None, f"cannot read ready line: {line}"
        if not line:
            return None, None, None, "child exited before printing a ready line"
        start_ms = (printed_at - started) * 1000.0
        time.sleep(idle_s)
        try:
            rss_kib, peak_kib = read_memory(process.pid, proc_root)
        except (OSError, ValueError) as exc:
            return start_ms, None, None, str(exc)
        return start_ms, rss_kib, peak_kib, None
    finally:
        stop_host(process)
        if not reader.is_alive():
            process.stdout.close()


def measure_runs(
    command: list[str], *, runs: int = 5, idle_s: float = 2.0,
    proc_root: str | Path = "/proc", ready_timeout_s: float = READY_TIMEOUT_S,
) -> list[tuple[float | None, int | None, int | None, str | None]]:
    """Measure sequential child processes."""
    return [
        measure_once(
            command, idle_s=idle_s, proc_root=proc_root, ready_timeout_s=ready_timeout_s,
        )
        for _ in range(runs)
    ]


def result_rows(results: list[tuple], command: list[str]) -> list[dict[str, str]]:
    """Summarize measurements without inventing values for missing samples."""
    context = (
        f"command={command!r}; kernel={platform.release()}; "
        "measured, one host, not a budget"
    )

    def row(name: str, index: int) -> dict[str, str]:
        values = [result[index] for result in results if result[index] is not None]
        if values:
            status = "ok"
            if index == 0:
                detail = (
                    f"median_ms={statistics.median(values):.3f}; "
                    f"range_ms={min(values):.3f}..{max(values):.3f}"
                )
            else:
                detail = f"median_kib={statistics.median(values):.3f}"
            detail += f"; successful_runs={len(values)}/{len(results)}"
        else:
            status = "unavailable"
            reasons = "; ".join(dict.fromkeys(
                result[3] for result in results if result[index] is None and result[3]
            ))
            detail = "reason=" + (reasons or "no successful measurement")
        return {"name": name, "status": status, "detail": f"{detail}; {context}"}

    return [row("host_start_ms", 0), row("host_idle_rss_kib", 1), row("host_peak_rss_kib", 2)]


def main(argv: list[str] | None = None, *, proc_root: str | Path = "/proc") -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--idle-s", type=float, default=2.0)
    parser.add_argument("--ready-timeout-s", type=float, default=READY_TIMEOUT_S)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    if not math.isfinite(args.idle_s) or args.idle_s < 0:
        parser.error("--idle-s must be finite and non-negative")
    if not math.isfinite(args.ready_timeout_s) or args.ready_timeout_s <= 0:
        parser.error("--ready-timeout-s must be finite and positive")
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    command = command or builtin_command()
    results = measure_runs(
        command, runs=args.runs, idle_s=args.idle_s, proc_root=proc_root,
        ready_timeout_s=args.ready_timeout_s,
    )
    for row in result_rows(results, command):
        print(json.dumps(row, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
