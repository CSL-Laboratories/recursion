"""Provider adapters. Headless CLIs, plus fixtures that never start a process.

Each live adapter builds argv and starts a process only when ``allow_live``
is set. The harness does not read or store credentials.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from scripts.v3.harness.errors import HarnessError, LiveProviderDisabled, ProviderTimeout


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
        if not request.cwd:
            raise HarnessError(f"{self.name} live runs require an explicit working folder")
        timeout = 120 if timeout_seconds is None else timeout_seconds
        prepared = self.prepare(request)
        invoke = _default_runner if runner is None else runner
        try:
            try:
                completed = invoke(list(prepared.argv), prepared.stdin_text, timeout, request.cwd)
            except subprocess.TimeoutExpired as exc:
                raise ProviderTimeout(f"{self.name} timed out") from exc
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
        return _grok_argv(request, "<prompt-file>")

    def prepare(self, request: ProviderRequest) -> _Prepared:
        path = _write_grok_prompt(request.task)

        def cleanup() -> None:
            path.unlink(missing_ok=True)

        return _Prepared(_grok_argv(request, str(path)), None, cleanup)


def _grok_argv(request: ProviderRequest, prompt_file: str) -> tuple[str, ...]:
    argv = list(GrokHeadlessProvider.STATIC_ARGV)
    argv.extend(["--prompt-file", prompt_file])
    if request.cwd:
        argv.extend(["--cwd", request.cwd])
    return tuple(argv)


def _write_grok_prompt(task: str) -> Path:
    """Write the prompt to a new owner-only file. The name is not predictable."""

    return _write_private_prompt(task, prefix="recursion-grok-", label="grok")


_PROMPT_ARGV_LIMIT = 7000
_GEMINI_LONG_PROMPT = " "
_OPENCODE_LONG_PROMPT = "Carry out the task in the attached file."


class GeminiHeadlessProvider(HeadlessCLI):
    """Gemini CLI headless mode.

    ``-p`` is the prompt. ``--output-format stream-json`` writes JSON events.
    ``--approval-mode yolo`` approves tool calls and ``--skip-trust`` accepts
    the workspace. A prompt that does not fit in argv is stdin, and ``-p`` is
    a single space so the flag still has a value. The process cwd is the
    working folder. The process starts only when ``allow_live`` is set.
    """

    name = "gemini"
    STATIC_ARGV = (
        "gemini",
        "-p",
        "--output-format",
        "stream-json",
        "--approval-mode",
        "yolo",
        "--skip-trust",
    )
    CAPABILITIES = ("execute", "stream-json", "subscription")

    def argv(self, request: ProviderRequest) -> tuple[str, ...]:
        prompt = request.task if _prompt_fits(request.task) else _GEMINI_LONG_PROMPT
        return _insert_after(self.STATIC_ARGV, "-p", prompt)

    def prepare(self, request: ProviderRequest) -> _Prepared:
        stdin = None if _prompt_fits(request.task) else request.task
        return _Prepared(self.argv(request), stdin, _noop)


class CursorHeadlessProvider(HeadlessCLI):
    """Cursor Agent print mode.

    The documented command is ``agent``. ``-p`` prints instead of opening the
    UI. ``--force`` allows commands and ``--trust`` accepts the workspace.
    ``--workspace`` is the working folder. A short prompt is the positional
    argument. A longer prompt is stdin. The process starts only when
    ``allow_live`` is set.
    """

    name = "cursor"
    STATIC_ARGV = ("agent", "-p", "--output-format", "stream-json", "--force", "--trust")
    CAPABILITIES = ("execute", "stream-json", "subscription")

    def argv(self, request: ProviderRequest) -> tuple[str, ...]:
        argv = _with_flag(self.STATIC_ARGV, "--workspace", request.cwd)
        if _prompt_fits(request.task):
            return argv + ("--", request.task)
        return argv

    def prepare(self, request: ProviderRequest) -> _Prepared:
        stdin = None if _prompt_fits(request.task) else request.task
        return _Prepared(self.argv(request), stdin, _noop)


class OpenCodeHeadlessProvider(HeadlessCLI):
    """OpenCode non-interactive mode: ``opencode run --format json --auto``.

    ``--format json`` writes one JSON event per line. ``--auto`` approves
    permissions that are not explicitly denied. ``--dir`` is the working
    folder. ``-p`` on this command is a password flag and is not used. A
    longer task is a private file passed with ``--file``. The process starts
    only when ``allow_live`` is set.
    """

    name = "opencode"
    STATIC_ARGV = ("opencode", "run", "--format", "json", "--auto")
    CAPABILITIES = ("execute", "json")

    def argv(self, request: ProviderRequest) -> tuple[str, ...]:
        argv = _with_flag(self.STATIC_ARGV, "--dir", request.cwd)
        if _prompt_fits(request.task):
            return argv + (request.task,)
        return argv + ("--file", "<prompt-file>", _OPENCODE_LONG_PROMPT)

    def prepare(self, request: ProviderRequest) -> _Prepared:
        argv = _with_flag(self.STATIC_ARGV, "--dir", request.cwd)
        if _prompt_fits(request.task):
            return _Prepared(argv + (request.task,), None, _noop)
        path = _write_private_prompt(request.task, prefix="recursion-opencode-", label="opencode")

        def cleanup() -> None:
            path.unlink(missing_ok=True)

        return _Prepared(argv + ("--file", str(path), _OPENCODE_LONG_PROMPT), None, cleanup)


# Launch-critical names from the accepted compatibility matrix. Claude, Grok,
# Cursor, Gemini, and OpenCode have a one-shot headless CLI behind
# ``--allow-live``. ChatGPT and Hermes do not: the interface named here is
# the one this slice does not speak.
LAUNCH_CRITICAL = {
    "ChatGPT": {"deferred": "codex app-server JSON-RPC over stdio (--listen stdio://)"},
    "Claude": {"adapter": "claude"},
    "Cursor": {"adapter": "cursor"},
    "Gemini": {"adapter": "gemini"},
    "Grok": {"adapter": "grok"},
    "Hermes": {"deferred": "TUI gateway JSON-RPC"},
    "OpenCode": {"adapter": "opencode"},
}


def adapters() -> dict[str, type[ProviderAdapter]]:
    """Registered adapter classes, keyed by provider name."""

    classes = (
        FixtureProvider,
        FixtureBProvider,
        ClaudeHeadlessProvider,
        CodexExecProvider,
        GrokHeadlessProvider,
        GeminiHeadlessProvider,
        CursorHeadlessProvider,
        OpenCodeHeadlessProvider,
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


def _prompt_fits(task: str) -> bool:
    """True when ``task`` can sit in argv without nearing the Windows limit."""

    return len(task) <= _PROMPT_ARGV_LIMIT


def _insert_after(argv: tuple[str, ...], flag: str, value: str) -> tuple[str, ...]:
    items = list(argv)
    items.insert(items.index(flag) + 1, value)
    return tuple(items)


def _with_flag(argv: tuple[str, ...], flag: str, value: str | None) -> tuple[str, ...]:
    if not value:
        return argv
    return argv + (flag, value)


def _write_private_prompt(task: str, *, prefix: str, label: str) -> Path:
    """Write the prompt to a new owner-only file. The name is not predictable."""

    try:
        fd, name = tempfile.mkstemp(prefix=prefix, suffix=".txt")
    except OSError as exc:
        raise HarnessError(f"{label} prompt file could not be created") from exc
    path = Path(name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        payload = memoryview(task.encode("utf-8"))
        while payload:
            written = os.write(fd, payload)
            if written <= 0:
                raise OSError("prompt write made no progress")
            payload = payload[written:]
        os.fsync(fd)
    except OSError as exc:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise HarnessError(f"{label} prompt file could not be written") from exc
    os.close(fd)
    return path


def _default_runner(argv: list[str], stdin_text: str | None, timeout: int, cwd: str | None):
    # CreateProcess only appends .exe, so resolve npm .cmd shims first.
    resolved = shutil.which(argv[0])
    if resolved is not None:
        argv = [resolved, *argv[1:]]
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
