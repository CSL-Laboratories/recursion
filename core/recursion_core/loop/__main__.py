"""``python -m recursion_core.loop``: start the loop as an engine process.

    python -m recursion_core.loop --provider anthropic --model <model id from the route> --home <engine home>
    python -m recursion_core.loop --provider fake --fake-script <steps.json> --home <engine home>

The key and the base URL come from the environment only (ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL), never from an
argument, a file or standard input. The key is removed from this process's environment as soon as the provider
holds it, so no child the loop starts (the search worker, a shell command, the acceptance check) inherits it;
and none of them gets the engine's environment anyway (each builds its own from a short list of names). On Linux
the key's text is also cleared from the environment block the process started with, which
``/proc/<pid>/environ`` shows to every process of the same user (DeepSeek F3): removing a name from the
environment does not clear those bytes. If they cannot be cleared there, the loop does not start.

The model has no default: it is the route's model, given by the driver. The engine home holds the spill folder;
it must be a private folder (created 0700 if missing). The workspace is the current folder unless --workspace
names another.

Write, edit and shell ask the core before they run and are denied when no answer comes by --approval-timeout.
``--permissions full_access`` skips the asking (grill Q20) only together with ``--acknowledge-full-access``,
which the surface passes when the user acknowledged it for this run. ``--acceptance <command>`` is the task's
acceptance check (done.py).
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

from recursion_core.loop import compaction
from recursion_core.loop import done as d
from recursion_core.loop import instructions as ins
from recursion_core.loop import permissions as perm
from recursion_core.loop import protocol as p
from recursion_core.loop import store as st
from recursion_core.loop.engine import DEFAULT_MAX_STEPS, Config, Engine, Session
from recursion_core.loop.providers import anthropic, fake
from recursion_core.loop.tools import edit, read, search, shell, write
from recursion_core.loop.tools.registry import DEFAULT_MAX_BYTES, DEFAULT_MAX_LINES, ToolRegistry
from recursion_core.loop.tools.workspace import SpillStore, ToolError, Workspace, check_private_dir

TOOLS = (read.TOOL, search.TOOL, write.TOOL, edit.TOOL, shell.TOOL)
STOP_WAIT_S = 5  # how long the main thread waits for the input thread to end after closed
INPUT_THREAD = "recursion-loop-stdin"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Keys the loop may find in its environment. Each one present is redacted from everything the loop writes and
# removed from its environment, whichever provider runs. (OPENAI_API_KEY is LOOP-OAI's provider's.)
KEY_ENV = (anthropic.ENV_KEY, "OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m recursion_core.loop", description=__doc__.splitlines()[0])
    ap.add_argument("--provider", choices=("anthropic", "fake"), required=True)
    ap.add_argument("--model", help="the route's model id (required for a real provider)")
    ap.add_argument("--home", required=True,
                    help="the engine's private home folder (on a POSIX file system: a Windows drive mounted in WSL "
                         "shows every folder as open to all and is refused)")
    ap.add_argument("--workspace", default=None, help="the folder the tools work in (default: the current folder)")
    ap.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    ap.add_argument("--max-output-tokens", type=int, default=64000)
    ap.add_argument("--effort", choices=EFFORTS, default=None)
    ap.add_argument("--output-cap-bytes", type=int, default=DEFAULT_MAX_BYTES)
    ap.add_argument("--output-cap-lines", type=int, default=DEFAULT_MAX_LINES)
    ap.add_argument("--permissions", choices=(perm.ASK, perm.FULL_ACCESS), default=perm.ASK)
    ap.add_argument("--acknowledge-full-access", action="store_true",
                    help="the user acknowledged the full-access profile for this run")
    ap.add_argument("--approval-timeout", type=float, default=perm.DEFAULT_DEADLINE_S,
                    help="seconds a permission request waits before it is denied")
    ap.add_argument("--acceptance", default=None, help="the task's acceptance check, a shell command")
    ap.add_argument("--acceptance-rounds", type=int, default=d.DEFAULT_ROUNDS)
    ap.add_argument("--no-project-instructions", action="store_true",
                    help="do not read AGENTS.md and CLAUDE.md into the prompt")
    ap.add_argument("--context-window", type=int, default=None,
                    help="the model's context window in tokens, from the route; without it the loop never compacts")
    ap.add_argument("--resume", default=None, metavar="ENGINE_SESSION_ID",
                    help="continue the conversation stored under this id in the engine home")
    ap.add_argument("--fake-script", help="steps for the fake provider (tests and conformance runs)")
    ap.add_argument("--fake-record", help="append each request the fake provider receives to this file")
    return ap


def _home(path: str) -> Path:
    home = Path(path)
    if not home.is_absolute():
        raise SystemExit("recursion_loop: --home must be an absolute path")
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        check_private_dir(home, "the engine home")
    except ToolError as exc:
        raise SystemExit(f"recursion_loop: {exc}") from None
    return home


def take_keys(environ) -> list[str]:
    """Remove every key named in KEY_ENV from ``environ`` and return their values, for the redactor."""

    taken = []
    for name in KEY_ENV:
        value = environ.pop(name, None)
        if value and len(value) >= 8:
            taken.append(value)
    return taken


def clear_start_environment(names=KEY_ENV) -> bool:
    """Linux: overwrite with NUL bytes the values of ``names`` in the environment block this process started
    with (the memory ``/proc/<pid>/environ`` reads, which unsetting a variable leaves as it was). Only that block
    is written, located from ``/proc/self/stat``. Returns False when it could not be done, or when a value is
    still readable there afterwards; True otherwise, and on a system without /proc."""

    if not sys.platform.startswith("linux"):
        return True
    wanted = {n.encode() for n in names}

    def readable() -> bool:
        with open("/proc/self/environ", "rb") as f:
            block = f.read()
        return any(name in wanted and value.strip(b"\0")
                   for name, _, value in (entry.partition(b"=") for entry in block.split(b"\0")))

    if not os.path.exists("/proc/self/environ"):
        return True  # no /proc: there is no such block to read, for this process or any other
    try:
        if not readable():
            return True  # nothing to clear
    except OSError:
        return False  # it could not be checked, so it is not reported as done (astra C3, DeepSeek N4)
    try:
        import ctypes

        with open("/proc/self/stat", "rb") as f:
            stat = f.read()
        fields = stat[stat.rfind(b")") + 2:].split()
        start, end = int(fields[47]), int(fields[48])  # env_start and env_end (fields 50 and 51)
        if not 0 < start < end:
            return False
        block = ctypes.string_at(start, end - start)
        at = 0
        for entry in block.split(b"\0"):
            name, sep, value = entry.partition(b"=")
            if sep and name in wanted and value:
                ctypes.memset(start + at + len(name) + 1, 0, len(value))
            at += len(entry) + 1
        return not readable()
    except (OSError, ValueError, IndexError, AttributeError):
        return False


def build(args: argparse.Namespace, env: dict[str, str]):
    if args.provider == "anthropic":
        if not args.model:
            raise SystemExit("recursion_loop: --model is required (the route names the model)")
        try:
            provider = anthropic.AnthropicProvider.from_env(env)
        except anthropic.ConfigError as exc:
            raise SystemExit(f"recursion_loop: {exc}") from None
        model = args.model
    else:
        if not args.fake_script:
            raise SystemExit("recursion_loop: --fake-script is required with --provider fake")
        provider = fake.FakeProvider.from_file(args.fake_script, record_path=args.fake_record)
        model = args.model or "fake-model"
    return provider, model


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.max_steps < 1 or args.max_output_tokens < 1:
        raise SystemExit("recursion_loop: --max-steps and --max-output-tokens must be positive")
    if args.permissions == perm.FULL_ACCESS and not args.acknowledge_full_access:
        raise SystemExit("recursion_loop: the full-access profile needs --acknowledge-full-access for this run")
    if args.approval_timeout <= 0:
        raise SystemExit("recursion_loop: --approval-timeout must be positive")
    try:
        acceptance = d.Acceptance(args.acceptance, rounds=args.acceptance_rounds) if args.acceptance else None
        if args.context_window is not None:
            compaction.ceiling(args.context_window)  # refuses a window too small for a tool session
    except ValueError as exc:
        raise SystemExit(f"recursion_loop: {exc}") from None
    provider, model = build(args, dict(os.environ))
    # The provider holds the key now; nothing else in this process, or any child of it, gets it.
    if not clear_start_environment():
        raise SystemExit("recursion_loop: the key could not be cleared from this process's start environment "
                         "(/proc/<pid>/environ); not starting")
    secrets = [s for s in getattr(provider, "secrets", ()) if s] + take_keys(os.environ)
    home = _home(args.home)
    try:
        workspace = Workspace(Path(args.workspace) if args.workspace else Path.cwd(), SpillStore(home))
    except ToolError as exc:
        raise SystemExit(f"recursion_loop: {exc}") from None
    try:
        registry = ToolRegistry(TOOLS, max_bytes=args.output_cap_bytes, max_lines=args.output_cap_lines)
    except ValueError as exc:
        raise SystemExit(f"recursion_loop: {exc}") from None
    text, found = ("", []) if args.no_project_instructions else ins.discover(workspace.root)
    redactor = p.Redactor(secrets)
    out = open(sys.stdout.fileno(), "w", encoding="utf-8", newline="\n", closefd=False, buffering=1)
    writer = p.Writer(out, redactor, seal_after="closed")  # nothing follows closed on standard output
    gate = perm.PermissionGate(writer, profile=args.permissions, deadline_s=args.approval_timeout)
    try:
        store = st.ConversationStore(home, args.resume or st.new_id(), redactor.scrub_obj)
        loaded = store.load() if args.resume else None
        if loaded is None:
            store.create()  # the id the ready line names can be resumed from now on (astra R11, sol R10)
        # A resumed conversation passes this run's secret policy here, or is refused (astra R8).
        session = Session(provider, registry, workspace, writer, redactor,
                          Config(model=model, max_output_tokens=args.max_output_tokens, max_steps=args.max_steps,
                                 effort=args.effort, acceptance=acceptance, instructions=text,
                                 context_window=args.context_window), gate=gate, store=store, loaded=loaded)
    except (st.StoreError, ToolError, OSError) as exc:
        raise SystemExit(f"recursion_loop: {exc}") from None
    engine = Engine(session, writer)
    writer.write("ready", protocol=p.PROTOCOL_VERSION, engine="recursion_loop", provider=provider.name, model=model,
                 tools=list(registry.names()), permissions=args.permissions,
                 acceptance=acceptance.command if acceptance else None,
                 instructions=[f.to_wire() for f in found], engine_session_id=store.session_id,
                 resumed=loaded is not None, unknown_outcomes=list(loaded.repaired) if loaded else [])

    try:
        return serve_input(engine, sys.stdin.fileno())
    finally:
        store.close()


def serve_input(engine: Engine, fd: int) -> int:
    """Run ``engine`` on the lines a thread of its own reads from ``fd``. On POSIX that thread has ended when
    this returns, so the interpreter never exits with it inside a read (``protocol.InputReader`` says why)."""

    reader = p.InputReader(fd)

    def pump() -> None:
        try:
            p.lines_from(reader.read, engine.feed_line, engine.line_too_long)
        except p.Stopped:
            pass
        finally:
            engine.input_ended()

    thread = threading.Thread(target=pump, name=INPUT_THREAD, daemon=True)
    thread.start()
    try:
        return engine.serve()
    finally:
        reader.stop()
        if reader.stoppable:
            thread.join(STOP_WAIT_S)
            if not thread.is_alive():
                reader.close()


if __name__ == "__main__":
    sys.exit(main())
