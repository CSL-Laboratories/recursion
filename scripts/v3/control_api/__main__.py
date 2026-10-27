"""Local operator entry point: ``python -m scripts.v3.control_api``.

``mcp-fake`` serves the Control MCP over stdio. ``direct-fake`` is the labeled
direct-CLI fallback (not X2). ``serve-fake`` runs the fake-driver Control API on loopback and writes its bearer
token to an owner-only file. The token is never printed. ``call`` and ``events``
read that file. No subcommand can reach a live provider.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading

from scripts.v3.control_api.auth import SCOPE_CONTROL, SCOPE_OBSERVE
from scripts.v3.control_api.client import (
    ControlClient,
    ControlRpcError,
    TokenFileRefused,
    create_token_file,
    read_token_file,
    unlink_created_token,
)
from scripts.v3.control_api.fake_engines import build_fake_stack
from scripts.v3.control_api.direct_cli import run_direct
from scripts.v3.control_api.http_server import ControlHTTPServer
from scripts.v3.control_api.mcp_server import ControlMcpServer


def _unlink_created(
    path: str | os.PathLike[str],
    identity: tuple[int, int] | None,
    descriptor: int | None,
) -> None:
    """Unlink path when it still names the file recorded at exclusive create."""

    unlink_created_token(path, identity, descriptor)


def _serve(args: argparse.Namespace, stop: threading.Event | None = None) -> int:
    stack = build_fake_stack()
    token = stack.issue("operator", args.tenant, {SCOPE_CONTROL, SCOPE_OBSERVE})
    server = None
    identity: tuple[int, int] | None = None
    held: int | None = None
    try:
        server = ControlHTTPServer(stack.service, "127.0.0.1", args.port)
        try:
            device, inode, held = create_token_file(args.token_file, token)
            identity = (device, inode)
        except FileExistsError:
            print(json.dumps({"error": "token_file_exists"}), file=sys.stderr)
            server.close()
            return 2
        server.start()
    except OSError:
        _unlink_created(args.token_file, identity, held)
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        raise
    if stop is None:
        stop = threading.Event()
    try:
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        print(json.dumps({"url": server.url, "tenant": args.tenant, "live_calls": False}), flush=True)
        # An untimed condition wait can delay the Python signal handler until
        # that wait returns (macOS does this). SIGTERM would then leave
        # serve-fake running. Returning to Python between short waits lets the
        # handler set ``stop``.
        while not stop.wait(0.2):
            pass
        server.stop()
    finally:
        _unlink_created(args.token_file, identity, held)
    return 0


def _client(args: argparse.Namespace) -> ControlClient:
    return ControlClient(args.url, read_token_file(args.token_file))


def _call(args: argparse.Namespace) -> int:
    params = json.loads(args.params) if args.params else {}
    status, response = _client(args).exchange(args.method, params)
    print(json.dumps({"http_status": status, "response": response}, sort_keys=True))
    return 0 if "error" not in response else 1


def _events(args: argparse.Namespace) -> int:
    print(json.dumps(_client(args).poll(args.after, wait_s=args.wait), sort_keys=True))
    return 0


def _mcp(args: argparse.Namespace) -> int:
    if args.url:
        if not args.token_file:
            raise ValueError("--url needs --token-file")
        client = _client(args)

        def dispatch(method: str, params: dict) -> dict:
            return client.exchange(method, params)[1]

    else:
        stack = build_fake_stack()
        token = stack.issue("operator", args.tenant, {SCOPE_CONTROL, SCOPE_OBSERVE})

        def dispatch(method: str, params: dict) -> dict:
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            return stack.service.handle(body, f"Bearer {token}")

    ControlMcpServer(dispatch).serve(sys.stdin, sys.stdout)
    return 0


def _direct(args: argparse.Namespace) -> int:
    print(json.dumps(run_direct(args.engine, args.prompt), sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.v3.control_api")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve-fake", help="serve the fake-driver API on loopback")
    serve.add_argument("--token-file", required=True)
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--tenant", default="tenant-demo")
    serve.set_defaults(handler=_serve)
    mcp = sub.add_parser("mcp-fake", help="Control MCP over stdio; fake driver only")
    mcp.add_argument("--tenant", default="tenant-demo")
    mcp.add_argument("--url")
    mcp.add_argument("--token-file")
    mcp.set_defaults(handler=_mcp)
    direct = sub.add_parser(
        "direct-fake",
        help="direct-CLI fallback: one fake run, bypasses the Control API, NOT X2",
    )
    direct.add_argument("--engine", required=True, choices=("claude", "codex"))
    direct.add_argument("--prompt", required=True)
    direct.set_defaults(handler=_direct)
    for name, handler in (("call", _call), ("events", _events)):
        command = sub.add_parser(name)
        command.add_argument("--url", required=True)
        command.add_argument("--token-file", required=True)
        command.set_defaults(handler=handler)
        if name == "call":
            command.add_argument("method")
            command.add_argument("params", nargs="?")
        else:
            command.add_argument("--after", type=int, default=0)
            command.add_argument("--wait", type=float, default=0.0)
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (TokenFileRefused, ControlRpcError, ValueError, OSError) as error:
        print(json.dumps({"error": type(error).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
