"""``python -m recursion_core.loop``: start the loop as an engine process.

    python -m recursion_core.loop --provider anthropic --model <model id from the route> --home <engine home>
    python -m recursion_core.loop --provider fake --fake-script <steps.json> --home <engine home>

The key and the base URL come from the environment only (ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL), never from an
argument, a file or standard input. The key is removed from this process's environment as soon as the provider
holds it, so no child the loop starts (the search worker, later the shell tool) inherits it.

The model has no default: it is the route's model, given by the driver. The engine home holds the spill folder;
it must be a private folder (created 0700 if missing). The workspace is the current folder unless --workspace
names another.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

from recursion_core.loop import protocol as p
from recursion_core.loop.engine import DEFAULT_MAX_STEPS, Config, Engine, Session
from recursion_core.loop.providers import anthropic, fake
from recursion_core.loop.tools import read, search
from recursion_core.loop.tools.registry import DEFAULT_MAX_BYTES, DEFAULT_MAX_LINES, ToolRegistry
from recursion_core.loop.tools.workspace import SpillStore, ToolError, Workspace, check_private_dir

TOOLS = (read.TOOL, search.TOOL)
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Keys the loop may find in its environment. Each one present is redacted from everything the loop writes and
# removed from its environment, whichever provider runs. (OPENAI_API_KEY is LOOP-OAI's provider's.)
KEY_ENV = (anthropic.ENV_KEY, "OPENAI_API_KEY")


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m recursion_core.loop", description=__doc__.splitlines()[0])
    ap.add_argument("--provider", choices=("anthropic", "fake"), required=True)
    ap.add_argument("--model", help="the route's model id (required for a real provider)")
    ap.add_argument("--home", required=True, help="the engine's private home folder")
    ap.add_argument("--workspace", default=None, help="the folder the tools work in (default: the current folder)")
    ap.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    ap.add_argument("--max-output-tokens", type=int, default=64000)
    ap.add_argument("--effort", choices=EFFORTS, default=None)
    ap.add_argument("--output-cap-bytes", type=int, default=DEFAULT_MAX_BYTES)
    ap.add_argument("--output-cap-lines", type=int, default=DEFAULT_MAX_LINES)
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
    provider, model = build(args, dict(os.environ))
    # The provider holds the key now; nothing else in this process, or any child of it, gets it.
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
    redactor = p.Redactor(secrets)
    out = open(sys.stdout.fileno(), "w", encoding="utf-8", newline="\n", closefd=False, buffering=1)
    writer = p.Writer(out, redactor)
    session = Session(provider, registry, workspace, writer, redactor,
                      Config(model=model, max_output_tokens=args.max_output_tokens, max_steps=args.max_steps,
                             effort=args.effort))
    engine = Engine(session, writer)
    writer.write("ready", protocol=p.PROTOCOL_VERSION, engine="recursion_loop", provider=provider.name, model=model,
                 tools=list(registry.names()))

    def pump() -> None:
        try:
            p.read_lines(sys.stdin.buffer, engine.feed_line, engine.line_too_long)
        finally:
            engine.input_ended()

    threading.Thread(target=pump, name="recursion-loop-stdin", daemon=True).start()
    return engine.serve()


if __name__ == "__main__":
    sys.exit(main())
