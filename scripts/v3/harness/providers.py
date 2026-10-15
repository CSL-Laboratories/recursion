"""Provider adapters. Headless CLIs, plus fixtures that never start a process.

Each live adapter builds argv and starts a process only when ``allow_live``
is set. The harness does not read or store credentials.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from scripts.v3.harness.errors import HarnessError, LiveProviderDisabled


_PROMPT_ID = re.compile(r"[^A-Za-z0-9_-]")


@dataclass(frozen=True)
class ProviderRequest:
    """One piece of work for an adapter.

    ``kind`` is ``execute`` for the task and ``review`` for a verdict.
    """

    task: str
    packet_id: str
    kind: str = "execute"
    cwd: str | None = None


@dataclass(frozen=True)
class ProviderResult:
    """Adapter output. ``live`` is false when no provider process was started."""

    provider: str
    argv: tuple[str, ...]
    text: str
    live: bool

    def to_dict(self) -> dict:
        return {
            "argv": list(self.argv),
            "live": self.live,
            "provider": self.provider,
            "text": self.text,
        }


@dataclass(frozen=True)
class _Prepared:
    argv: tuple[str, ...]
    stdin_text: str | None
    cleanup: object


class ProviderAdapter:
    """Headless execution behind one interface."""

    name = ""
    STATIC_ARGV: tuple[str, ...] = ()
    CAPABILITIES: tuple[str, ...] = ()
    OFFLINE = False

    def argv(self, request: ProviderRequest) -> tuple[str, ...]:
        raise NotImplementedError

    def execute(
        self,
        request: ProviderRequest,
        *,
        allow_live: bool = False,
        timeout_seconds: int | None = None,
        runner=None,
    ) -> ProviderResult:
        raise NotImplementedError


class FixtureProvider(ProviderAdapter):
    """Deterministic stand-in. It never starts a process and needs no account."""

    name = "fixture"
    STATIC_ARGV = ("fixture-provider", "--output-format", "json")
    CAPABILITIES = ("execute", "deterministic")
    OFFLINE = True

    def argv(self, request: ProviderRequest) -> tuple[str, ...]:
        del request
        return self.STATIC_ARGV

    def execute(
        self,
        request: ProviderRequest,
        *,
        allow_live: bool = False,
        timeout_seconds: int | None = None,
        runner=None,
    ) -> ProviderResult:
        del allow_live, timeout_seconds, runner
        if request.kind == "review":
            text = _fixture_verdict(self.name, request.task)
        else:
            text = "\n".join(
                (
                    f"provider={self.name}",
                    f"packet={request.packet_id}",
                    request.task,
                )
            )
        return ProviderResult(provider=self.name, argv=self.argv(request), text=text, live=False)


class FixtureBProvider(FixtureProvider):
    """Second offline fixture, so a run can be reviewed without a live CLI."""

    name = "fixture-b"
    STATIC_ARGV = ("fixture-b-provider", "--output-format", "json")


class HeadlessCLI(ProviderAdapter):
    """Shared process start for subscription CLIs."""

    def execute(
        self,
        request: ProviderRequest,
        *,
        allow_live: bool = False,
        timeout_seconds: int | None = None,
        runner=None,
    ) -> ProviderResult:
        if not allow_live:
            raise LiveProviderDisabled(
                f"{self.name} headless CLI is not started unless --allow-live is set"
            )
        timeout = 120 if timeout_seconds is None else timeout_seconds
        prepared = self.prepare(request)
        invoke = _default_runner if runner is None else runner
        try:
            try:
                completed = invoke(list(prepared.argv), prepared.stdin_text, timeout, request.cwd)
            except subprocess.TimeoutExpired as exc:
                raise HarnessError(f"{self.name} timed out") from exc
            except FileNotFoundError as exc:
                raise HarnessError(f"{self.name} CLI was not found") from exc
        finally:
            prepared.cleanup()
        if not isinstance(completed, subprocess.CompletedProcess):
            raise HarnessError(f"{self.name} runner returned no result")
        if completed.returncode != 0:
            raise HarnessError(f"{self.name} exited {completed.returncode}")
        return ProviderResult(
            provider=self.name,
            argv=prepared.argv,
            text=completed.stdout or "",
            live=True,
        )

    def prepare(self, request: ProviderRequest) -> _Prepared:
        raise NotImplementedError


class ClaudeHeadlessProvider(HeadlessCLI):
    """Claude Code print mode. The prompt is stdin. The user's own sign-in is used."""

    name = "claude"
    STATIC_ARGV = (
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
    )
    CAPABILITIES = ("execute", "stream-json", "subscription")

    def argv(self, request: ProviderRequest) -> tuple[str, ...]:
        del request
        return self.STATIC_ARGV

    def prepare(self, request: ProviderRequest) -> _Prepared:
        return _Prepared(self.argv(request), request.task, _noop)


class CodexExecProvider(HeadlessCLI):
    """Codex noninteractive mode: ``codex exec --json --approve-for-me``.

    The prompt is stdin, passed as ``-``. ``--cd`` is the workspace when one is set.
    """

    name = "codex"
    STATIC_ARGV = ("codex", "exec", "--json", "--approve-for-me")
    CAPABILITIES = ("execute", "jsonl", "subscription")

    def argv(self, request: ProviderRequest) -> tuple[str, ...]:
        argv = list(self.STATIC_ARGV)
        if request.cwd:
            argv.extend(["--cd", request.cwd])
        argv.append("-")
        return tuple(argv)

    def prepare(self, request: ProviderRequest) -> _Prepared:
        return _Prepared(self.argv(request), request.task, _noop)


class GrokHeadlessProvider(HeadlessCLI):
    """Grok headless mode.

    The prompt is a temp file (``--prompt-file``). ``--output-format
    streaming-json`` writes one JSON event per line. The file is removed
    after the process returns.
    """

    name = "grok"
    STATIC_ARGV = (
        "grok",
        "--output-format",
        "streaming-json",
        "--always-approve",
        "--no-auto-update",
    )
    CAPABILITIES = ("execute", "streaming-json", "subscription")

    def argv(self, request: ProviderRequest) -> tuple[str, ...]:
        argv = list(self.STATIC_ARGV)
        argv.extend(["--prompt-file", str(grok_prompt_path(request.packet_id))])
        if request.cwd:
            argv.extend(["--cwd", request.cwd])
        return tuple(argv)

    def prepare(self, request: ProviderRequest) -> _Prepared:
        path = grok_prompt_path(request.packet_id)
        path.write_text(request.task, encoding="utf-8")

        def cleanup() -> None:
            path.unlink(missing_ok=True)

        return _Prepared(self.argv(request), None, cleanup)


def grok_prompt_path(packet_id: str) -> Path:
    safe = _PROMPT_ID.sub("", packet_id)[:80] or "task"
    return Path(tempfile.gettempdir()) / f"recursion-grok-{safe}.txt"


def adapters() -> dict[str, type[ProviderAdapter]]:
    """Registered adapter classes, keyed by provider name."""

    classes = (
        FixtureProvider,
        FixtureBProvider,
        ClaudeHeadlessProvider,
        CodexExecProvider,
        GrokHeadlessProvider,
    )
    return {cls.name: cls for cls in classes}


def open_provider(name: str) -> ProviderAdapter:
    """Return the adapter registered under ``name``."""

    known = adapters()
    try:
        return known[name]()
    except KeyError as exc:
        names = ", ".join(sorted(known))
        raise HarnessError(f"unknown provider {name!r}; known providers: {names}") from exc


def _fixture_verdict(name: str, task: str) -> str:
    """Approve a review. ``fixture: revise once`` asks for a single revise first."""

    if "fixture: revise once" in task and "Review notes:" not in task:
        return f"VERDICT revise\n{name} asked for one revise"
    return f"VERDICT approve\n{name} review"


def _noop() -> None:
    return None


def _default_runner(argv: list[str], stdin_text: str | None, timeout: int, cwd: str | None):
    return subprocess.run(
        argv,
        input=stdin_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=timeout,
        cwd=cwd,
    )
