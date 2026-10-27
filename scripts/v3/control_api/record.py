"""Recorded fake-driver run through the Control API (WP16 spine evidence).

Drives one scripted scenario over real loopback HTTP against the fake stack:
the Claude-Code-style path and the Codex-style path, one repair, one limit and
breaker, and four refused callers. Output is a pure function of the code, so
the committed recording must equal a fresh run byte for byte.

    python -X utf8 -B -m scripts.v3.control_api.record --check
    python -X utf8 -B -m scripts.v3.control_api.record --write

Nothing here reaches a provider. Tokens are minted in memory per run and are
never part of the recording. Replay needs the CI-pinned ``jsonschema``
(``requirements-ci.txt``), which supplies ``Draft202012Validator``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from pathlib import Path
from typing import Any

from scripts.v3.control_api.auth import SCOPE_CONTROL, SCOPE_OBSERVE
from scripts.v3.control_api.client import ControlClient
from scripts.v3.control_api.fake_engines import build_fake_stack
from scripts.v3.control_api.http_server import ControlHTTPServer

ROOT = Path(__file__).resolve().parents[3]
RECORDING_PATH = ROOT / "scripts" / "tests" / "v3" / "control_api" / "recordings" / "spine-fake-run.json"
RECORDING_TYPE = "ControlApiRecording@0"
PROMPT = "Review the change and answer with a verdict."
REPAIR_PROMPT = "Review the second change and answer with a verdict."
SCRIPTS = {"claude": ["ok", "not_json"], "codex": ["ok", "limit"]}


class _Recorder:
    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []

    def step(
        self,
        label: str,
        caller: str,
        client: ControlClient,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        status, response = client.exchange(method, params)
        self.steps.append(
            {
                "step": len(self.steps) + 1,
                "label": label,
                "caller": caller,
                "request": {"method": method, "params": params or {}},
                "http_status": status,
                "response": response,
            }
        )
        return response

    def poll(self, label: str, client: ControlClient, caller: str) -> None:
        page = client.poll(0)
        self.steps.append(
            {
                "step": len(self.steps) + 1,
                "label": label,
                "caller": caller,
                "request": {"method": "GET /v1/events", "params": {"after": 0}},
                "http_status": 200,
                "response": page,
            }
        )


def run_scenario() -> dict[str, Any]:
    stack = build_fake_stack(scripts=SCRIPTS)
    recorder = _Recorder()
    with ControlHTTPServer(stack.service) as server:
        operator = ControlClient(server.url, stack.issue("operator", "tenant-demo", {SCOPE_CONTROL, SCOPE_OBSERVE}))
        viewer = ControlClient(server.url, stack.issue("viewer", "tenant-demo", {SCOPE_OBSERVE}))
        outsider = ControlClient(server.url, stack.issue("outsider", "tenant-other", {SCOPE_CONTROL, SCOPE_OBSERVE}))
        anonymous = ControlClient(server.url, None)
        forged = ControlClient(server.url, "x" * 43)

        step = recorder.step
        step("service info", "operator", operator, "service.info")
        step("route list", "operator", operator, "route.list")
        for engine, session in (("claude", "demo-claude"), ("codex", "demo-codex")):
            step(f"open {engine} session", "operator", operator, "session.open", {"engine": engine, "session": session})
            step(
                f"run {engine} (ok)",
                "operator",
                operator,
                "session.run",
                {"engine": engine, "session": session, "prompt": PROMPT},
            )
        step("open claude repair session", "operator", operator, "session.open", {"engine": "claude", "session": "demo-repair"})
        step(
            "run claude (invalid output, one repair, ok)",
            "operator",
            operator,
            "session.run",
            {"engine": "claude", "session": "demo-repair", "prompt": REPAIR_PROMPT},
        )
        step(
            "run codex (limit hit, breaker opens)",
            "operator",
            operator,
            "session.run",
            {"engine": "codex", "session": "demo-codex", "prompt": REPAIR_PROMPT},
        )
        step(
            "run codex again (breaker open, not dispatched)",
            "operator",
            operator,
            "session.run",
            {"engine": "codex", "session": "demo-codex", "prompt": REPAIR_PROMPT},
        )
        step("refused: no credentials", "anonymous", anonymous, "service.info")
        step("refused: forged token", "forged", forged, "session.get", {"engine": "claude", "session": "demo-claude"})
        step(
            "refused: viewer cannot run",
            "viewer",
            viewer,
            "session.run",
            {"engine": "claude", "session": "demo-claude", "prompt": PROMPT},
        )
        step(
            "refused: other tenant cannot see the session",
            "outsider",
            outsider,
            "session.get",
            {"engine": "claude", "session": "demo-claude"},
        )
        step("claude session state", "viewer", viewer, "session.get", {"engine": "claude", "session": "demo-claude"})
        step("codex session state", "viewer", viewer, "session.get", {"engine": "codex", "session": "demo-codex"})
        recorder.poll("observer journal", viewer, "viewer")
    transports = {engine: len(t.calls) for engine, t in stack.transports.items()}
    body = {
        "type": RECORDING_TYPE,
        "driver": "fake",
        "live_calls": False,
        "engine_paths": ["claude", "codex"],
        "transport_calls": transports,
        "steps": recorder.steps,
    }
    body["transcript_sha256"] = transcript_digest(body)
    return body


def transcript_digest(body: dict[str, Any]) -> str:
    material = {key: value for key, value in body.items() if key != "transcript_sha256"}
    text = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def render(body: dict[str, Any]) -> str:
    return json.dumps(body, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def environment() -> dict[str, str]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.v3.control_api.record")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="fresh run must equal the committed recording")
    mode.add_argument("--write", action="store_true", help="rewrite the committed recording")
    parser.add_argument("--path", default=str(RECORDING_PATH))
    args = parser.parse_args(argv)
    fresh = render(run_scenario())
    path = Path(args.path)
    if args.write:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(fresh, encoding="utf-8", newline="\n")
    committed = path.read_text(encoding="utf-8") if path.exists() else None
    matches = committed == fresh
    digest = json.loads(fresh)["transcript_sha256"]
    print(
        json.dumps(
            {
                "recording": path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path),
                "matches_committed": matches,
                "transcript_sha256": digest,
                "environment": environment(),
            },
            sort_keys=True,
        )
    )
    return 0 if matches else 1


if __name__ == "__main__":
    sys.exit(main())
