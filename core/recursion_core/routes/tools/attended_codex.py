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

Mode ``run`` is the founder-attended run of the session driver (``routes/engines/codex.py``), for Lucas,
signed in with his own ``codex login`` (the tool never reads, copies or asks for a credential; the child's
environment keeps only the names Codex needs to find its own login and drops every API-key name, so the
run uses that login). It starts ONE app-server worker in a scratch working folder, and turns what is
typed into driver calls:

    text           a user message: a new turn, or, while a turn is in progress, a steer into it
    y / n          approve / deny the oldest approval the engine asked for (shown in full first)
    /interrupt     the protocol's turn interrupt
    /stop          end the worker (standard input ended), keeping the thread id
    /resume        a new worker that resumes that thread (thread/resume)
    /quit          end the worker and the tool

Everything is captured under ``--out``: ``version.txt`` (``codex --version``), and per worker
``worker-N.stdout.jsonl``, ``worker-N.stdin.jsonl``, ``worker-N.stderr.txt`` and ``worker-N.drafts.jsonl``
(what the driver made of each line). The steps Lucas runs, in a scratch git folder:
"list the files", then "count them"; /stop, then /resume and one more message; ask Codex to create a
file, n, ask again, y; while a turn runs, list the worker's child processes (the tool prints the pid
and the command): no updater may appear; /quit. The captures become the driver's fixtures (PR 4).

    python core/recursion_core/routes/tools/attended_codex.py run --workdir <scratch git folder> \\
        --out <scratch capture folder> [--codex <codex binary>] [--model <model>]

This mode uses a plain local launcher in the working folder; the daemon's own launcher (the brief's
"through the daemon's launcher in a scratch state folder") is Linux-only and not wired here yet.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
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


# -- mode run ---------------------------------------------------------------------------------------------
# Names a Codex child may inherit for the founder run: what it needs to find its own login and run tools.
# Every API-key name is left out, so the run uses Codex's own login (Lucas, Nov 8: "Codex's own login only").
CHILD_ENV_NAMES = ("PATH", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
                   "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR", "LANG",
                   "LC_ALL", "LC_CTYPE", "TERM", "TZ", "CODEX_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                   "XDG_STATE_HOME", "XDG_RUNTIME_DIR")
_EOF = object()


def child_env(parent: dict) -> dict:
    return {k: v for k, v in parent.items() if k.upper() in CHILD_ENV_NAMES}


class LocalProcess:
    """routes.launch's EngineProcess over a plain child process, capturing both directions."""

    def __init__(self, argv: list[str], env: dict, cwd: str, out: Path, tag: str) -> None:
        self._stdout = (out / f"{tag}.stdout.jsonl").open("w", encoding="utf-8", newline="\n")
        self._stdin = (out / f"{tag}.stdin.jsonl").open("w", encoding="utf-8", newline="\n")
        self._stderr = (out / f"{tag}.stderr.txt").open("wb")
        # Binary pipes: each line is exactly its UTF-8 bytes and one LF, on Windows too (no CRLF translation).
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr,
                                     cwd=cwd, env=env)
        self._lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            self._stdout.write(line + "\n")
            self._stdout.flush()
            self._lines.put(line)
        self._stdout.close()
        self.proc.stdout.close()
        self._lines.put(_EOF)

    def send(self, line: str) -> None:
        self._stdin.write(line + "\n")
        self._stdin.flush()
        self.proc.stdin.write(line.encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def close_stdin(self) -> None:
        if not self.proc.stdin.closed:
            self.proc.stdin.close()

    def read_line(self, timeout_s: float):
        from recursion_core.routes.launch import ReadTimeout

        try:
            item = self._lines.get(timeout=max(timeout_s, 0.001))
        except queue.Empty:
            raise ReadTimeout() from None
        return None if item is _EOF else item

    def fence(self, reason: str) -> None:
        """End the worker (the founder run's stand-in for the daemon's fence) and close the captures."""
        self.close_stdin()
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self._stdin.close()
        self._stderr.close()


class Console:
    """The approvals port for the founder run: it keeps each request and the person at the keyboard answers."""

    def __init__(self, say) -> None:
        self.say, self.pending, self.count = say, [], 0

    def request(self, effect: dict, binding: dict) -> str:
        self.count += 1
        approval_id = f"apr-attended-{self.count}"
        self.pending.append((approval_id, effect))
        self.say(f"APPROVAL {approval_id}: {json.dumps(effect, indent=1, sort_keys=True)}\n  answer y or n")
        return approval_id


def session_loop(driver, commands, say, console, *, env: dict | None = None, session_id: str = "ses-attended",
                 route: dict | None = None, read_s: float = 1.0) -> dict:
    """Run the founder's commands against ``driver``. Returns what it saw, for the capture and for tests.

    After each command it reads the worker's output until the turn has ended or an approval waits for an
    answer, so a steer is not offered here (``input()`` is not read during a turn)."""

    from recursion_core.routes.driver import DriverRefused, DriverStateError
    from recursion_core.routes.launch import ReadTimeout

    route = route or {"engine": "codex", "model": None, "connection": {"kind": "subscription", "id": "own-login"}}
    seen = {"threads": [], "drafts": [], "workers": 0}
    state = {"handle": None, "turn": 0, "delivery": 0}

    def start(esid):
        seen["workers"] += 1
        record = {"session_id": session_id, "run_id": f"attended-{seen['workers']}", "workspace": None,
                  "route": route, "engine_session_id": esid}
        call = driver.start if esid is None else driver.resume
        state["handle"] = call(record, env or {})
        seen["threads"].append(state["handle"].engine_session_id)
        say(f"worker {seen['workers']} on thread {state['handle'].engine_session_id}")

    def pump():
        handle = state["handle"]
        while True:
            try:
                drafts = driver.pump(handle, read_s)
            except ReadTimeout:
                if handle.live_turn is None or console.pending:
                    return
                continue
            if drafts is None:
                say("the worker ended its output")
                return
            for d in drafts:
                seen["drafts"].append(d)
                say(f"{d['type']}: {json.dumps(d['payload'], sort_keys=True)}")

    def step(line: str) -> bool:
        handle = state["handle"]
        if line in ("y", "n") and console.pending:
            approval_id, _ = console.pending.pop(0)
            driver.send(handle, {"kind": "approval_answer", "approval_id": approval_id,
                                 "decision": "approve" if line == "y" else "deny"})
        elif line == "/interrupt":
            if handle.live_turn is not None:
                driver.interrupt(handle, handle.live_turn)
        elif line in ("/stop", "/quit"):
            driver.send(handle, {"kind": "close"})
            pump()
            console.pending.clear()
            return line == "/quit"
        elif line == "/resume":
            start(seen["threads"][-1])
            return False
        else:
            state["delivery"] += 1
            if handle.live_turn is None:
                state["turn"] += 1
            driver.send(handle, {"kind": "user_message", "turn_id": handle.live_turn or f"turn-{state['turn']}",
                                 "text": line, "delivery_id": f"dlv-{state['delivery']}"})
        pump()
        return False

    start(None)
    for raw in commands:
        line = raw.strip()
        if not line:
            continue
        try:
            if step(line):
                break
        except (DriverRefused, DriverStateError) as err:
            say(f"refused: {err}")
    return seen


def run_mode(args: argparse.Namespace) -> int:
    from recursion_core.routes.engines.codex import CodexDriver

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "version.txt").write_text(_run([args.codex, "--version"]) + "\n", encoding="utf-8", newline="\n")
    workers = []

    def launcher(argv, *, env, shell=False):
        proc = LocalProcess(argv, env=env, cwd=args.workdir, out=out, tag=f"worker-{len(workers) + 1}")
        workers.append(proc)
        pid = proc.proc.pid
        tree = (f"Get-CimInstance Win32_Process -Filter ParentProcessId={pid}" if os.name == "nt"
                else f"ps --forest -o pid,ppid,args -g $(ps -o sid= -p {pid})")
        print(f"[worker pid {pid}; list its children with: {tree}]")
        return proc

    drafts = (out / "drafts.jsonl").open("w", encoding="utf-8", newline="\n")

    def say(text: str) -> None:
        print(text, flush=True)

    console = Console(say)
    driver = CodexDriver(launcher, time.monotonic, binary=args.codex, approvals=console)
    route = {"engine": "codex", "model": args.model, "connection": {"kind": "subscription", "id": "own-login"}}

    def typed():
        while True:
            try:
                yield input("> ")
            except EOFError:
                yield "/quit"
                return

    seen = session_loop(driver, typed(), say, console, env=child_env(dict(os.environ)), route=route)
    for d in seen["drafts"]:
        drafts.write(json.dumps(d, sort_keys=True) + "\n")
    drafts.close()
    for proc in workers:
        proc.fence("attended_run_over")
    print(f"captured in {out}: threads {seen['threads']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    schema = sub.add_parser("schema", help="record the installed app-server protocol as a fixture")
    schema.add_argument("--out", required=True, help="the fixtures folder (routes/engines/fixtures/codex)")
    schema.add_argument("--codex", default="codex", help="the codex command (default: codex)")
    schema.add_argument("--native", help="the native codex binary, to check the driver's literal texts")
    run = sub.add_parser("run", help="the founder-attended run of the session driver")
    run.add_argument("--workdir", required=True, help="a scratch git folder the worker runs in")
    run.add_argument("--out", required=True, help="a scratch folder for the captures")
    run.add_argument("--codex", default="codex", help="the codex binary (default: codex)")
    run.add_argument("--model", default=None, help="a model to ask for (default: Codex's own)")
    args = parser.parse_args(argv)
    return schema_mode(args) if args.mode == "schema" else run_mode(args)


if __name__ == "__main__":
    sys.exit(main())
