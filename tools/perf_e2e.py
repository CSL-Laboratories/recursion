#!/usr/bin/env python3
"""Provider-free end-to-end turn profile (W1).

Replays the CSL-84 stream fixtures through HeadlessCLI's injected runner.
The child is this interpreter, not a provider CLI. Phases are spawn, first
event, stream parse, validate, and persist. No numeric budget is applied.

    python -X utf8 -B tools/perf_e2e.py --smoke --out /tmp/e2e.json
    python -X utf8 -B tools/perf_e2e.py --fixtures claude-stream-json --out /tmp/e2e.json
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import platform
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import resource
except ImportError:  # Windows stdlib has no resource module.
    resource = None

TOOLS_DIR = Path(__file__).resolve().parent
ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from perf_bench import install_network_guard  # noqa: E402
from scripts.v3.harness.providers import ProviderRequest, open_provider  # noqa: E402
from scripts.v3.harness.review import parse_verdict  # noqa: E402
from scripts.v3.harness.state import new_run_id, save_run  # noqa: E402
from scripts.v3.harness.streams import assistant_text, claude_last_message  # noqa: E402
from scripts.v3.harness.usage import (  # noqa: E402
    add_usage,
    call_fits,
    default_budgets,
    empty_usage,
    exceeded,
    parse_usage,
)

SCHEMA = "recursion-perf-e2e/1"
WORKLOAD_SCHEMA = "recursion-perf-e2e-workload/1"
WORKLOAD_PATH = TOOLS_DIR / "perf_e2e.workload.json"
PHASES = ("spawn", "first_event", "stream_parse", "validate", "persist", "total")
P99_MIN_N = 100
CHILD_TIMEOUT_S = 30
PROMPT = "Replay the recorded stream. Do not call a provider."
_VERDICT_LINE = re.compile(
    r"^(?:[*_`]+)?\s*verdict\s*:?\s*(approve|revise)\b[.!]?\s*(?:[*_`]+)?\s*$",
    re.IGNORECASE,
)
_OVERHEAD_NOTE = (
    "harness_overhead_ns is fixture total median minus trivial spawn median only. "
    "The fixture's first event, parse, validate, and persist stay in that result, "
    "and the trivial child's non-spawn time is not removed. It is not adapter-only overhead. "
    "trivial_total_median_ns is reported beside the two operands."
)
DIMENSION_NAMES = (
    "authority, safety, privacy, effects",
    "verified quality",
    "recovery and replay",
    "owner/operator cost",
    "quota, tokens and money",
    "context/cache efficiency",
    "latency and throughput",
    "host resources",
    "terminal interaction",
    "portability/configurability",
    "code and maintenance surface",
    "extensibility/idle cost",
)
_NETWORK_MARK = "must not open network"


class PerfE2EError(RuntimeError):
    """The profile could not finish a turn. Timing noise is not this error."""


def lf_normalise(data: bytes) -> bytes:
    """Rewrite CRLF to LF. Fixture eol is unspecified, so Windows checkouts differ."""
    return data.replace(b"\r\n", b"\n")


def sha256_lf(data: bytes) -> str:
    return hashlib.sha256(lf_normalise(data)).hexdigest()


def require_digest(path: Path, expected: str) -> bytes:
    """Return LF-normalised bytes or raise when the digest does not match."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PerfE2EError(f"missing fixture {path}: {exc}") from exc
    digest = sha256_lf(data)
    if digest != expected:
        raise PerfE2EError(
            f"fixture digest mismatch for {path.name}: expected {expected} got {digest}"
        )
    return lf_normalise(data)


def exit_code(report: dict) -> int:
    """Timing values, including an unstable pilot, do not fail the process."""
    del report
    return 0


def load_workload(root: Path | None = None) -> dict:
    path = WORKLOAD_PATH if root is None else Path(root) / "tools" / "perf_e2e.workload.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PerfE2EError(f"cannot read {path.name}: {exc}") from exc
    _validate_workload(payload)
    return payload


def _validate_workload(payload: dict) -> None:
    if payload.get("schema") != WORKLOAD_SCHEMA:
        raise PerfE2EError(f"workload schema must be {WORKLOAD_SCHEMA}")
    fixtures = payload.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise PerfE2EError("workload fixtures are missing")
    seen = set()
    for spec in fixtures:
        name = spec.get("name")
        if not isinstance(name, str) or not name or name in seen:
            raise PerfE2EError("fixture name is missing or repeated")
        seen.add(name)
        for key in ("path", "provider", "evidence", "sha256_lf"):
            if not isinstance(spec.get(key), str) or not spec[key]:
                raise PerfE2EError(f"{name} is missing {key}")
        if spec["evidence"] not in {"synthetic", "source-documented"}:
            raise PerfE2EError(f"{name} evidence class is not synthetic or source-documented")
        if len(spec["sha256_lf"]) != 64:
            raise PerfE2EError(f"{name} sha256_lf is not 64 hex characters")
        try:
            int(spec["sha256_lf"], 16)
        except ValueError as exc:
            raise PerfE2EError(f"{name} sha256_lf is not hex") from exc
    hosts = payload.get("hosts")
    if not isinstance(hosts, list) or not {
        "linux-x64",
        "windows-11-x64",
        "macos-arm",
    } <= {row.get("id") for row in hosts}:
        raise PerfE2EError("workload hosts must include linux-x64, windows-11-x64, and macos-arm")
    for host in hosts:
        if host.get("measured") is not False:
            raise PerfE2EError(f"{host.get('id')} must stay measured false until a floor is qualified")
    pilot = payload.get("pilot") or {}
    if pilot.get("rounds") != 5 or pilot.get("Z") != 1.96 or pilot.get("W") != 0.05:
        raise PerfE2EError("pilot rounds, Z, and W must stay at 5, 1.96, and 0.05")
    proposed = payload.get("proposed_by_agent") or {}
    if proposed.get("proposed_by_agent") is not True:
        raise PerfE2EError("proposed_by_agent block is missing")
    cap = proposed.get("pilot_cap") or {}
    bound = proposed.get("stability_bound") or {}
    if not isinstance(cap.get("max_measured_turns"), int) or cap["max_measured_turns"] < 1:
        raise PerfE2EError("pilot cap turns must be a positive integer")
    if not isinstance(cap.get("max_seconds_per_fixture"), (int, float)) or cap["max_seconds_per_fixture"] <= 0:
        raise PerfE2EError("pilot cap seconds must be positive")
    if not isinstance(bound.get("relative_iqr_max"), (int, float)) or bound["relative_iqr_max"] <= 0:
        raise PerfE2EError("stability bound must be positive")
    dimensions = payload.get("dimensions")
    if not isinstance(dimensions, list) or [row.get("name") for row in dimensions] != list(DIMENSION_NAMES):
        raise PerfE2EError("workload dimensions must be the twelve PERFORMANCE-PLAN rows, in order")
    for row in dimensions:
        if not isinstance(row.get("observable_provider_free"), bool):
            raise PerfE2EError(f"{row.get('name')} is missing observable_provider_free")
        reason = row.get("null_reason")
        if row["observable_provider_free"]:
            if reason is not None:
                raise PerfE2EError(f"{row['name']} is observable and must not carry a null_reason")
        elif not isinstance(reason, str) or not reason.strip():
            raise PerfE2EError(f"{row['name']} needs a null_reason")


def _header(payload: bytes) -> dict[str, str]:
    found = {}
    for line in payload.splitlines():
        text = line.decode("utf-8", "replace").strip()
        if not text.startswith("#"):
            break
        key, sep, value = text[1:].partition(":")
        if sep:
            found[key.strip()] = value.strip()
    return found


def _select_fixtures(workload: dict, names: str) -> list[dict]:
    fixtures = workload["fixtures"]
    if not names.strip():
        return list(fixtures)
    wanted = [part.strip() for part in names.split(",")]
    if any(not part for part in wanted):
        raise PerfE2EError("fixtures list has an empty name")
    by_name = {spec["name"]: spec for spec in fixtures}
    chosen = []
    for name in wanted:
        stem = name[:-4] if name.endswith(".txt") else name
        spec = by_name.get(stem)
        if spec is None:
            known = ", ".join(by_name)
            raise PerfE2EError(f"unknown fixture {name!r}; known fixtures: {known}")
        chosen.append(spec)
    return chosen


def _load_fixture(root: Path, spec: dict) -> bytes:
    payload = require_digest(root / spec["path"], spec["sha256_lf"])
    header = _header(payload)
    if header.get("provider") != spec["provider"]:
        raise PerfE2EError(
            f"{spec['name']} header provider {header.get('provider')!r} != {spec['provider']}"
        )
    if header.get("evidence") != spec["evidence"]:
        raise PerfE2EError(
            f"{spec['name']} header evidence {header.get('evidence')!r} != {spec['evidence']}"
        )
    return payload


def _replay_script(payload: bytes) -> str:
    # One physical line so Windows CreateProcess does not see a raw newline
    # inside the -c argument. The child still writes and flushes each line.
    encoded = base64.b64encode(payload).decode("ascii")
    return (
        "import base64,sys; "
        f"raw=base64.b64decode('{encoded}'); "
        "out=sys.stdout.buffer; "
        "[(out.write(line if line.endswith(b'\\n') else line+b'\\n'), out.flush()) "
        "for line in raw.splitlines(True)]"
    )


def _child_argv(payload: bytes) -> list[str]:
    return [sys.executable, "-X", "utf8", "-B", "-c", _replay_script(payload)]


def _times() -> object | None:
    try:
        return os.times()
    except (AttributeError, OSError):
        return None


def _rss(usage) -> int | None:
    # usage is the per-child rusage from os.wait4. RUSAGE_CHILDREN is a
    # process-wide high-water mark and would leak earlier children's peaks.
    if resource is None or usage is None:
        return None
    return int(usage.ru_maxrss)


def _reap(proc, deadline: float):
    """Wait for proc and return its own rusage, or None where wait4 is absent."""
    if not hasattr(os, "wait4"):
        proc.wait(timeout=max(deadline - time.monotonic(), 0.001))
        return None
    while True:
        pid, status, usage = os.wait4(proc.pid, os.WNOHANG)
        if pid:
            proc.returncode = os.waitstatus_to_exitcode(status)
            return usage
        if time.monotonic() > deadline:
            raise subprocess.TimeoutExpired(proc.args, 0)
        time.sleep(0.001)


def _rss_unit() -> str | None:
    if resource is None:
        return None
    if sys.platform == "darwin":
        return "bytes"
    return "kilobytes"


def _child_cpu(before_times, after_times, platform_name: str) -> tuple[float | None, float | None, str | None]:
    """Child CPU deltas, or null where os.times does not report them.

    CPython documents that on Windows only user and system are known and the
    other os.times attributes are zero. Those zeros are not a measurement.
    """
    if before_times is None or after_times is None:
        return None, None, "os.times children fields are not available on this interpreter"
    if platform_name == "win32":
        return (
            None,
            None,
            "os.times children_user and children_system are always 0 on Windows; "
            "only user and system are known, so child CPU is not a measurement",
        )
    return (
        after_times.children_user - before_times.children_user,
        after_times.children_system - before_times.children_system,
        None,
    )


def _host_sample(before_times, after_times, rss_after: int | None) -> dict:
    user, system, cpu_reason = _child_cpu(before_times, after_times, sys.platform)
    if rss_after is None:
        rss_reason = (
            "resource.getrusage is not available on this platform; "
            "RSS was not read from a non-stdlib package"
        )
    else:
        rss_reason = None
    return {
        "rss_max": rss_after,
        "rss_unit": _rss_unit(),
        "rss_null_reason": rss_reason,
        "child_user_s": user,
        "child_system_s": system,
        "cpu_null_reason": cpu_reason,
    }


def _spawn_replay(payload: bytes, *, cwd: str, timeout: int):
    argv = _child_argv(payload)
    times_before = _times()
    proc = None
    try:
        t_spawn0 = time.perf_counter_ns()
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
        )
        t_spawn1 = time.perf_counter_ns()
        assert proc.stdout is not None and proc.stderr is not None
        deadline = time.monotonic() + timeout
        chunks: list[bytes] = []
        first_at = None
        while True:
            if time.monotonic() > deadline:
                raise PerfE2EError("replay child timed out before stdout closed")
            line = proc.stdout.readline()
            if line == b"":
                break
            chunks.append(line)
            if first_at is None and line.strip() and not line.lstrip().startswith(b"#"):
                first_at = time.perf_counter_ns()
        drain_at = time.perf_counter_ns()
        err = proc.stderr.read()
        try:
            usage = _reap(proc, time.monotonic() + timeout)
        except subprocess.TimeoutExpired as exc:
            raise PerfE2EError("replay child timed out") from exc
        rc = proc.returncode
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    if first_at is None:
        raise PerfE2EError("replay child wrote no non-empty stdout line")
    if rc != 0:
        detail = err.decode("utf-8", "replace")[:200]
        raise PerfE2EError(f"replay child exited {rc}: {detail}")
    text = b"".join(chunks).decode("utf-8", "replace")
    return {
        "spawn_ns": t_spawn1 - t_spawn0,
        "first_event_ns": first_at - t_spawn1,
        "stream_drain_ns": drain_at - first_at,
        "stdout": text,
        "host": _host_sample(times_before, _times(), _rss(usage)),
    }


def _make_runner(payload: bytes, box: dict):
    def runner(argv, stdin_text, timeout, cwd):
        # Provider argv is the live CLI. This profile never executes it.
        del argv, stdin_text
        child = _spawn_replay(payload, cwd=cwd or ".", timeout=int(timeout))
        box["child"] = child
        return subprocess.CompletedProcess(
            [sys.executable, "-X", "utf8", "-B", "-c"],
            0,
            stdout=child["stdout"],
            stderr="",
        )

    return runner


def _stream_parse(raw: str) -> dict:
    try:
        text = assistant_text(raw)
        last = claude_last_message(raw)
        usage = add_usage(empty_usage(), parse_usage(raw))
        verdict, notes = parse_verdict(raw)
    except PerfE2EError:
        raise
    except Exception as exc:
        raise PerfE2EError(f"parser exception: {type(exc).__name__}: {exc}") from exc
    return {
        "assistant_len": len(text),
        "last_len": None if last is None else len(last),
        "usage": usage,
        "verdict": verdict,
        "notes_len": len(notes),
        "token": _expected_verdict_token(text, last),
    }


def _clean_verdict_line(line: str) -> str:
    text = line.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'`":
        text = text[1:-1].strip()
    return text


def _expected_verdict_token(text: str, last: str | None) -> str | None:
    """Last approve/revise token in the text parse_verdict judges, or None."""
    body = text if last is None else last
    found = None
    for line in body.splitlines():
        match = _VERDICT_LINE.fullmatch(_clean_verdict_line(line))
        if match is not None:
            found = match.group(1).lower()
    return found


def _validate(parsed: dict, prompt: str) -> dict:
    try:
        budgets = default_budgets()
        fits = call_fits(parsed["usage"], budgets, prompt)
        over = exceeded(parsed["usage"], budgets)
        token = parsed["token"]
        if parsed["verdict"] not in {"approve", "revise"}:
            raise PerfE2EError(f"verdict {parsed['verdict']!r} is not approve or revise")
        if token is None:
            if parsed["verdict"] != "revise":
                raise PerfE2EError("a stream with no verdict token did not fall back to revise")
        elif parsed["verdict"] != token:
            raise PerfE2EError(f"verdict token {token} was parsed as {parsed['verdict']}")
    except PerfE2EError:
        raise
    except Exception as exc:
        raise PerfE2EError(f"validate exception: {type(exc).__name__}: {exc}") from exc
    return {
        "call_fits": bool(fits),
        "exceeded": bool(over),
        "verdict_token": token,
        "verdict_token_exercised": token is not None,
    }


def _as_error(exc: Exception) -> PerfE2EError:
    if isinstance(exc, PerfE2EError):
        return exc
    text = str(exc)
    if _NETWORK_MARK in text:
        return PerfE2EError(f"network attempt: {text}")
    return PerfE2EError(f"harness failure: {type(exc).__name__}: {text}")


def _persist(state_dir: Path, *, name: str, provider: str, parsed: dict, checked: dict) -> None:
    record = {
        "run_id": new_run_id(),
        "fixture": name,
        "provider": provider,
        "verdict": parsed["verdict"],
        "notes_len": parsed["notes_len"],
        "usage": parsed["usage"],
        "call_fits": checked["call_fits"],
        "exceeded": checked["exceeded"],
        "provider_calls": 0,
    }
    save_run(state_dir, record)


def _complete_sample(t0: int, child: dict, state_dir: Path, *, name: str, provider: str) -> dict:
    parse_0 = time.perf_counter_ns()
    parsed = _stream_parse(child["stdout"])
    parse_1 = time.perf_counter_ns()
    checked = _validate(parsed, PROMPT)
    validate_1 = time.perf_counter_ns()
    _persist(state_dir, name=name, provider=provider, parsed=parsed, checked=checked)
    persist_1 = time.perf_counter_ns()
    return {
        "spawn_ns": child["spawn_ns"],
        "first_event_ns": child["first_event_ns"],
        "stream_drain_ns": child["stream_drain_ns"],
        "stream_parse_ns": parse_1 - parse_0,
        "validate_ns": validate_1 - parse_1,
        "persist_ns": persist_1 - validate_1,
        "total_ns": persist_1 - t0,
        "verdict": parsed["verdict"],
        "verdict_token": checked["verdict_token"],
        "verdict_token_exercised": checked["verdict_token_exercised"],
        "call_fits": checked["call_fits"],
        "exceeded": checked["exceeded"],
        "host": child["host"],
    }


def _measure_fixture(adapter, payload: bytes, state_dir: Path, *, name: str, provider: str) -> dict:
    box: dict = {}
    request = ProviderRequest(
        task=PROMPT,
        packet_id="perf-e2e",
        kind="execute",
        cwd=str(state_dir),
    )
    t0 = time.perf_counter_ns()
    try:
        result = adapter.execute(
            request,
            allow_live=True,
            timeout_seconds=CHILD_TIMEOUT_S,
            runner=_make_runner(payload, box),
        )
    except PerfE2EError:
        raise
    except Exception as exc:
        raise _as_error(exc) from exc
    child = box.get("child")
    if child is None:
        raise PerfE2EError("runner did not record a child")
    if not isinstance(result.text, str) or not result.text.strip():
        raise PerfE2EError("harness returned empty stdout")
    child["stdout"] = result.text
    return _complete_sample(t0, child, state_dir, name=name, provider=provider)


def _measure_baseline(state_dir: Path) -> dict:
    t0 = time.perf_counter_ns()
    try:
        child = _spawn_replay(b"ok\n", cwd=str(state_dir), timeout=CHILD_TIMEOUT_S)
    except PerfE2EError:
        raise
    except Exception as exc:
        raise _as_error(exc) from exc
    return _complete_sample(t0, child, state_dir, name="trivial_spawn", provider="baseline")


def _ns(value: float) -> int:
    return int(round(value))


def _percentile(values: list[int], percent: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * (percent / 100.0)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return float(ordered[lo])
    weight = rank - lo
    return ordered[lo] * (1.0 - weight) + ordered[hi] * weight


def summarize_phase(values: list[int]) -> dict:
    if not values:
        raise PerfE2EError("phase has no samples")
    median = statistics.median(values)
    median_ns = _ns(median)
    p75 = _percentile(values, 75)
    p25 = _percentile(values, 25)
    iqr_ns = _ns(p75 - p25)
    mad = statistics.median([abs(median - sample) for sample in values])
    mad_ns = _ns(mad)
    if median_ns == 0:
        relative = None
        relative_reason = "relative IQR is null because the median is 0"
        mad_ratio = None
    else:
        relative = iqr_ns / median_ns
        relative_reason = None
        mad_ratio = mad_ns / median_ns
    n = len(values)
    if n >= P99_MIN_N:
        p99 = _ns(_percentile(values, 99))
        p99_reason = None
    else:
        p99 = None
        p99_reason = "p99 is null because n < 100"
    return {
        "n": n,
        "median_ns": median_ns,
        "p50_ns": median_ns,
        "p95_ns": _ns(_percentile(values, 95)),
        "p99_ns": p99,
        "p99_null_reason": p99_reason,
        "iqr_ns": iqr_ns,
        "relative_iqr": relative,
        "relative_iqr_null_reason": relative_reason,
        "mad_ns": mad_ns,
        "mad_over_median": mad_ratio,
    }


def _required_n(samples: list[int], z: float, w: float) -> int | None:
    if len(samples) < 2:
        return None
    mean = statistics.fmean(samples)
    if mean <= 0:
        return None
    sigma = statistics.stdev(samples) / mean
    return math.ceil((4 * z * z * sigma * sigma) / (w * w))


def _collect(measure, *, smoke: bool, pilot_rounds: int, cap_turns: int, cap_s: float, z: float, w: float, bound: float):
    started = time.perf_counter()
    samples: list[dict] = []
    if smoke:
        for _ in range(3):
            samples.append(measure())
        return samples, {
            "mode": "smoke",
            "pilot_rounds": None,
            "required_n": None,
            "required_n_null_reason": "smoke skips the pilot",
            "stop": "smoke",
            "outcome": "smoke",
            "elapsed_s": time.perf_counter() - started,
        }

    def capped() -> str | None:
        if len(samples) >= cap_turns:
            return "cap_turns"
        if samples and (time.perf_counter() - started) >= cap_s:
            return "cap_time"
        return None

    while len(samples) < pilot_rounds:
        stop = capped()
        if stop:
            break
        samples.append(measure())
    required = _required_n([sample["total_ns"] for sample in samples], z, w)
    need = pilot_rounds if required is None else max(pilot_rounds, required)
    stop = "required_n"
    while len(samples) < need:
        hit = capped()
        if hit:
            stop = hit
            break
        samples.append(measure())
    else:
        stop = "required_n"
    totals = summarize_phase([sample["total_ns"] for sample in samples])
    relative = totals["relative_iqr"]
    reached = len(samples) >= need
    if reached:
        outcome = "stable"
    elif relative is not None and relative > bound:
        outcome = "unstable"
    else:
        outcome = "capped"
    reason = None if required is not None else "required N is null because the pilot has fewer than 2 totals"
    return samples, {
        "mode": "pilot",
        "pilot_rounds": min(pilot_rounds, len(samples)),
        "required_n": required,
        "required_n_null_reason": reason,
        "stop": stop,
        "outcome": outcome,
        "elapsed_s": time.perf_counter() - started,
        "relative_iqr_max": bound,
    }


def _phases(samples: list[dict]) -> dict:
    rows = {}
    for name in PHASES:
        key = "total_ns" if name == "total" else f"{name}_ns"
        rows[name] = summarize_phase([sample[key] for sample in samples])
    return rows


def _host_summary(samples: list[dict]) -> dict:
    first = samples[0]["host"]
    rss_values = [sample["host"]["rss_max"] for sample in samples if sample["host"]["rss_max"] is not None]
    user = [sample["host"]["child_user_s"] for sample in samples if sample["host"]["child_user_s"] is not None]
    system = [sample["host"]["child_system_s"] for sample in samples if sample["host"]["child_system_s"] is not None]
    return {
        "rss_max": max(rss_values) if rss_values else None,
        "rss_unit": first["rss_unit"],
        "rss_null_reason": first["rss_null_reason"],
        "child_user_s_median": statistics.median(user) if user else None,
        "child_system_s_median": statistics.median(system) if system else None,
        "cpu_null_reason": first["cpu_null_reason"],
    }


def _row(name: str, samples: list[dict], pilot: dict, *, extra: dict | None = None) -> dict:
    row = {
        "name": name,
        "n": len(samples),
        "pilot": pilot,
        "phases": _phases(samples),
        "host": _host_summary(samples),
        "samples": samples,
    }
    if extra:
        row.update(extra)
    return row


def _overhead(fixture_total: int, trivial_spawn: int, trivial_total: int) -> dict:
    return {
        "fixture_total_median_ns": fixture_total,
        "trivial_spawn_median_ns": trivial_spawn,
        "trivial_total_median_ns": trivial_total,
        "harness_overhead_ns": fixture_total - trivial_spawn,
        "expression": "fixture_total_median_ns - trivial_spawn_median_ns",
        "note": _OVERHEAD_NOTE,
    }


def _git_commit(root: Path) -> dict:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "null_reason": "git rev-parse HEAD failed"}
    commit = result.stdout.strip()
    if not commit:
        return {"commit": None, "null_reason": "git rev-parse HEAD returned an empty commit"}
    return {"commit": commit, "null_reason": None}


def _ci_flag() -> dict:
    return {
        "GITHUB_ACTIONS": os.environ.get("GITHUB_ACTIONS") == "true",
        "CI": os.environ.get("CI") == "true",
        "RUNNER_OS": os.environ.get("RUNNER_OS"),
        "RUNNER_ARCH": os.environ.get("RUNNER_ARCH"),
    }


def _overall_outcome(outcomes: list[str]) -> str:
    if outcomes and all(item == "smoke" for item in outcomes):
        return "smoke"
    if "unstable" in outcomes:
        return "unstable"
    if "capped" in outcomes:
        return "capped"
    return "stable"


def run_profile(root: Path, *, smoke: bool, fixture_names: str) -> dict:
    install_network_guard()
    root = root.resolve()
    workload = load_workload()
    specs = _select_fixtures(workload, fixture_names)
    loaded = [(spec, _load_fixture(root, spec)) for spec in specs]
    proposed = workload["proposed_by_agent"]
    pilot = workload["pilot"]
    cap_turns = proposed["pilot_cap"]["max_measured_turns"]
    cap_s = float(proposed["pilot_cap"]["max_seconds_per_fixture"])
    bound = float(proposed["stability_bound"]["relative_iqr_max"])
    common = {
        "smoke": smoke,
        "pilot_rounds": int(pilot["rounds"]),
        "cap_turns": cap_turns,
        "cap_s": cap_s,
        "z": float(pilot["Z"]),
        "w": float(pilot["W"]),
        "bound": bound,
    }
    with tempfile.TemporaryDirectory(prefix="perf-e2e-") as tmp:
        state_dir = Path(tmp)
        baseline_samples, baseline_pilot = _collect(
            lambda: _measure_baseline(state_dir),
            **common,
        )
        trivial_spawn = summarize_phase([sample["spawn_ns"] for sample in baseline_samples])["median_ns"]
        trivial_total = summarize_phase([sample["total_ns"] for sample in baseline_samples])["median_ns"]
        fixtures = []
        for spec, payload in loaded:
            adapter = open_provider(spec["provider"])
            samples, pilot_row = _collect(
                lambda spec=spec, payload=payload, adapter=adapter: _measure_fixture(
                    adapter,
                    payload,
                    state_dir,
                    name=spec["name"],
                    provider=spec["provider"],
                ),
                **common,
            )
            total_median = summarize_phase([sample["total_ns"] for sample in samples])["median_ns"]
            fixtures.append(
                _row(
                    spec["name"],
                    samples,
                    pilot_row,
                    extra={
                        "path": spec["path"],
                        "provider": spec["provider"],
                        "evidence": spec["evidence"],
                        "sha256_lf": spec["sha256_lf"],
                        "overhead": _overhead(total_median, trivial_spawn, trivial_total),
                    },
                )
            )
        baseline = _row("trivial_spawn", baseline_samples, baseline_pilot)
    outcomes = [baseline_pilot["outcome"], *[row["pilot"]["outcome"] for row in fixtures]]
    git = _git_commit(root)
    return {
        "schema": SCHEMA,
        "provider_calls": 0,
        "provider_argv_executed": False,
        "child_flags": ["-X", "utf8", "-B", "-c"],
        "workload": WORKLOAD_PATH.name,
        "workload_schema": WORKLOAD_SCHEMA,
        "mode": "smoke" if smoke else "pilot",
        "metadata": {
            "git_commit": git["commit"],
            "git_commit_null_reason": git["null_reason"],
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "ci": _ci_flag(),
            "pilot_outcome": _overall_outcome(outcomes),
            "hosts_measured": False,
            "hosts_measured_null_reason": "This run does not qualify a D-028 floor. Host profiles stay measured false.",
        },
        "baseline": baseline,
        "fixtures": fixtures,
        "dimensions": workload["dimensions"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="write the JSON report here")
    parser.add_argument("--smoke", action="store_true", help="3 turns per row, no pilot")
    parser.add_argument("--fixtures", default="", help="comma-separated fixture names; default is all")
    args = parser.parse_args(argv)
    try:
        report = run_profile(ROOT, smoke=args.smoke, fixture_names=args.fixtures)
    except PerfE2EError as exc:
        print(f"PERF_E2E_FAIL: {exc}", file=sys.stderr)
        return 1
    text = json.dumps(report, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(text)
    print(
        f"PERF_E2E outcome={report['metadata']['pilot_outcome']} fixtures={len(report['fixtures'])}",
        file=sys.stderr,
    )
    return exit_code(report)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"PERF_E2E_FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
