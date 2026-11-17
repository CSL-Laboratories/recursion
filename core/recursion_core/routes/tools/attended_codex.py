"""Founder-attended Codex tool (lane DRIVER-CX). Run by Lucas, or by a lane for the ``schema`` mode only.

Mode ``schema`` records what the installed Codex says its own app-server protocol is, so that no method or
field name in ``routes/codex.py`` rests on memory or on a source map. It runs two commands of the installed
binary and nothing else: ``codex --version`` and the binary's own generator,
``codex -c check_for_update_on_startup=false app-server generate-json-schema --out <scratch>``. Neither
signs in, reads a credential or starts a worker. It then writes one extract,
``app-server-<version>.json``, into the fixtures folder: every method name the protocol defines, in its
four tables, and a summary (required keys, property names and types, enum values) of each definition the
Codex driver touches. With ``--native <path>`` it also records whether the native binary contains the
exact literal texts the driver matches on (today one: the ChatGPT-auth error of the rate-limit read).

    python core/recursion_core/routes/tools/attended_codex.py schema \\
        --out core/recursion_core/routes/engines/fixtures/codex [--codex codex] [--native <codex binary>]

The founder-attended run of the driver (two turns, a resume, an approval denied then approved, the
process tree, stdout captured as fixtures) is a later mode of this tool and is not built yet.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import subprocess
import sys
import tempfile
from pathlib import Path

NO_UPDATE = ("-c", "check_for_update_on_startup=false")  # the same override routes/codex.py launches with
TABLES = {
    "client_requests": "ClientRequest.json",
    "client_notifications": "ClientNotification.json",
    "server_notifications": "ServerNotification.json",
    "server_requests": "ServerRequest.json",
}
# Definitions the Codex driver sends, reads or must answer. A name the installed protocol lacks is recorded
# under "missing", so a later version that drops one shows up as a fixture change, not as a silent gap.
DEFINITIONS = (
    "InitializeParams", "ClientInfo", "InitializeResponse", "JSONRPCErrorError",
    "GetAccountRateLimitsParams", "GetAccountRateLimitsResponse", "RateLimitSnapshot", "RateLimitWindow",
    "RateLimitReachedType",
    "AccountRateLimitsUpdatedNotification",
    "ThreadStartParams", "ThreadStartResponse", "ThreadResumeParams", "ThreadResumeResponse", "Thread",
    "ThreadStatus", "TurnStartParams", "TurnStartResponse", "UserInput", "TurnSteerParams",
    "TurnSteerResponse", "TurnInterruptParams", "TurnInterruptResponse",
    "TurnStartedNotification", "TurnCompletedNotification", "Turn", "TurnStatus", "TurnError",
    "CodexErrorInfo", "ErrorNotification", "ItemStartedNotification", "ItemCompletedNotification",
    "ThreadItem", "AgentMessageDeltaNotification", "ThreadTokenUsageUpdatedNotification", "ThreadTokenUsage",
    "TokenUsageBreakdown", "ModelReroutedNotification", "ServerRequestResolvedNotification",
    "AskForApproval", "SandboxMode", "SandboxPolicy",
    "CommandExecutionRequestApprovalParams", "CommandExecutionRequestApprovalResponse",
    "CommandExecutionApprovalDecision", "FileChangeRequestApprovalParams", "FileChangeRequestApprovalResponse",
    "FileChangeApprovalDecision", "PermissionsRequestApprovalParams", "PermissionsRequestApprovalResponse",
    "ToolRequestUserInputParams", "McpServerElicitationRequestParams", "DynamicToolCallParams",
    "ChatgptAuthTokensRefreshParams", "ApplyPatchApprovalParams", "ExecCommandApprovalParams",
)
# Literal texts routes/codex.py compares a vendor value with, checked against the native binary.
LITERALS = ("chatgpt authentication required to read rate limits",)


def _brief(node: object) -> object:
    """A compact, deterministic description of one schema node (no descriptions, no examples)."""

    if node is True:
        return "any"
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        return "ref:" + node["$ref"].rsplit("/", 1)[-1]
    if "allOf" in node and len(node["allOf"]) == 1:
        return _brief(node["allOf"][0])
    for key in ("anyOf", "oneOf"):
        if key in node:
            return {key: [_brief(item) for item in node[key]]}
    if "enum" in node:
        return {"enum": list(node["enum"])}
    if "const" in node:
        return {"enum": [node["const"]]}
    out: dict = {}
    if "type" in node:
        out["type"] = node["type"]
    if "properties" in node:
        out["properties"] = {name: _brief(sub) for name, sub in sorted(node["properties"].items())}
        out["required"] = sorted(node.get("required", []))
    if "items" in node:
        out["items"] = _brief(node["items"])
    return out or "any"


def _method_table(doc: dict) -> dict:
    table = {}
    for variant in doc.get("oneOf", []):
        props = variant.get("properties", {})
        method = props.get("method", {})
        names = method.get("enum") or [method.get("const")]
        params = props.get("params")
        table[names[0]] = _brief(params) if params is not None else None
    return dict(sorted(table.items()))


def _all_definitions(schema_dir: Path) -> dict:
    """Every named definition in the bundle, plus each top-level file's own schema under its title."""

    found: dict = {}
    for path in sorted(schema_dir.rglob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        for name, sub in (doc.get("definitions") or {}).items():
            found.setdefault(name, sub)
        title = doc.get("title")
        if isinstance(title, str) and "oneOf" not in doc and title not in found:
            found[title] = doc
    return found


def extract(schema_dir: Path, version: str, *, experimental: bool = False, literals: dict | None = None,
            bundle_files: tuple[str, ...] = ("codex_app_server_protocol.schemas.json",
                                             "codex_app_server_protocol.v2.schemas.json")) -> dict:
    """The fixture: method tables and definition summaries read from a generated schema folder."""

    definitions = _all_definitions(schema_dir)
    digests = {}
    for name in (*bundle_files, *TABLES.values()):
        path = schema_dir / name
        if path.is_file():
            digests[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "codex_version": version,
        "generator": ["codex", *NO_UPDATE, "app-server", "generate-json-schema",
                      *(["--experimental"] if experimental else []), "--out", "<scratch>"],
        "experimental": experimental,
        "sha256": digests,
        **{key: _method_table(json.loads((schema_dir / name).read_text(encoding="utf-8")))
           for key, name in TABLES.items()},
        "definitions": {name: _brief(definitions[name]) for name in DEFINITIONS if name in definitions},
        "missing": [name for name in DEFINITIONS if name not in definitions],
        "binary_literals": literals or {},
    }


def scan_literals(native: Path, texts: tuple[str, ...] = LITERALS) -> dict:
    """Whether each text occurs byte for byte in the native binary, with the binary's identity."""

    digest = hashlib.sha256()
    with native.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            found = {text: mm.find(text.encode("utf-8")) >= 0 for text in texts}
    return {"binary": native.name, "size": native.stat().st_size, "sha256": digest.hexdigest(), "found": found}


def _run(argv: list[str]) -> str:
    done = subprocess.run(argv, capture_output=True, text=True, check=True, timeout=300)
    return done.stdout.strip()


def schema_mode(args: argparse.Namespace) -> int:
    version = _run([args.codex, "--version"])
    with tempfile.TemporaryDirectory(prefix="codex-schema-") as scratch:
        _run([args.codex, *NO_UPDATE, "app-server", "generate-json-schema", "--out", scratch])
        literals = scan_literals(Path(args.native)) if args.native else None
        doc = extract(Path(scratch), version, literals=literals)
    tag = version.split()[-1]
    out = Path(args.out) / f"app-server-{tag}.json"
    out.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {out} ({len(doc['client_requests'])} client requests, {len(doc['definitions'])} definitions, "
          f"missing {doc['missing']})")
    return 0


def main(argv: list[str] | None = None) -> int:
    from recursion_core.std_fds import occupy_standard_fds

    occupy_standard_fds()  # before any open: a hole at fd 0, 1 or 2 would be taken by the next pipe
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    schema = sub.add_parser("schema", help="record the installed app-server protocol as a fixture")
    schema.add_argument("--out", required=True, help="the fixtures folder (routes/engines/fixtures/codex)")
    schema.add_argument("--codex", default="codex", help="the codex command (default: codex)")
    schema.add_argument("--native", help="the native codex binary, to check the driver's literal texts")
    args = parser.parse_args(argv)
    return schema_mode(args)


if __name__ == "__main__":
    # The documented command names this file. Its directory is not the package root, so the one
    # helper (core/recursion_core/std_fds.py) is not importable until that root is on the path.
    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    sys.exit(main())
