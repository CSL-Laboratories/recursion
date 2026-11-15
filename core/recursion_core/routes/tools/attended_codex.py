"""Founder-attended Codex tool (lane DRIVER-CX). Run by Lucas, or by a lane for the ``schema`` mode only.

Mode ``schema`` records what the installed Codex says its own app-server protocol is, so that no method or
field name in ``routes/codex.py`` rests on memory or on a source map. It runs two commands of the installed
binary and nothing else: ``codex --version`` and the binary's own generator,
``codex -c check_for_update_on_startup=false app-server generate-json-schema --out <scratch>``. Neither
signs in, reads a credential or starts a worker, and both get the environment mode ``run`` gives its worker
(below). It then writes one extract,
``app-server-<version>.json``, into the fixtures folder: every method name the protocol defines, in its
four tables, and a summary (required keys, property names and types, enum values) of each definition the
Codex driver touches. With ``--native <path>`` it also records whether the native binary contains the
exact literal texts the driver matches on (today one: the ChatGPT-auth error of the rate-limit read).

    python core/recursion_core/routes/tools/attended_codex.py schema \\
        --out core/recursion_core/routes/engines/fixtures/codex [--codex codex] [--native <codex binary>]

Mode ``run`` is the founder-attended run of the session driver (``routes/engines/codex.py``), for Lucas,
signed in with his own ``codex login`` (the tool never reads, copies or asks for a credential; every child
it starts, the worker and ``codex --version`` alike, gets an environment that keeps only the names Codex
needs to find its own login and drops every API-key name, so the run uses that login). It starts ONE
app-server worker in a scratch working folder, and turns what is
typed into driver calls:

    text           a user message: a new turn, or, while a turn is in progress, a steer into it
    y / n          approve / deny the oldest approval the engine asked for (shown in full first)
    /interrupt     the protocol's turn interrupt
    /stop          end the worker (standard input ended), keeping the thread id
    /resume        a new worker that resumes that thread (thread/resume); only after /stop
    /quit          end the worker and the tool

Commands are read while a turn runs too, so /interrupt and /stop work without waiting for the turn.
Everything is captured under ``--out``: ``version.txt`` (``codex --version``), per worker
``worker-N.stdout.jsonl``, ``worker-N.stdin.jsonl`` and ``worker-N.stderr.txt``, and for the whole run one
``drafts.jsonl`` (what the driver made of each line). Every worker is ended and every
capture closed however the run ends, an exception or Ctrl-C included. The steps Lucas runs, in a scratch git folder:
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
    # DEFINITIONS and every definition they refer to, however deep (the #402 read, GPT-6 Astra R5), so a
    # value can be checked all the way down. A name referred to but not in the bundle is recorded as missing.
    briefs: dict = {}
    missing: list = []
    queue = list(DEFINITIONS)
    while queue:
        name = queue.pop(0)
        if name in briefs or name in missing:
            continue
        if name not in definitions:
            missing.append(name)
            continue
        briefs[name] = _brief(definitions[name])
        queue.extend(sorted(_refs(briefs[name])))
    return {
        "codex_version": version,
        "generator": ["codex", *NO_UPDATE, "app-server", "generate-json-schema",
                      *(["--experimental"] if experimental else []), "--out", "<scratch>"],
        "experimental": experimental,
        "sha256": digests,
        **{key: _method_table(json.loads((schema_dir / name).read_text(encoding="utf-8")))
           for key, name in TABLES.items()},
        "definitions": dict(sorted(briefs.items())),
        "missing": missing,
        "binary_literals": literals or {},
    }


def _refs(node: object) -> set:
    """The definition names a summary refers to (``ref:<name>``), at any depth."""

    if isinstance(node, str):
        return {node[4:]} if node.startswith("ref:") else set()
    if isinstance(node, dict):
        return set().union(*(_refs(v) for v in node.values())) if node else set()
    if isinstance(node, list):
        return set().union(*(_refs(v) for v in node)) if node else set()
    return set()


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
    """One command of the installed binary (``--version``, the schema generator), with the environment the
    run's worker gets: no API-key name reaches it (the #402 reads, round 4, DeepSeek R4-3)."""

    done = subprocess.run(argv, env=child_env(dict(os.environ)), capture_output=True, text=True, check=True,
                          timeout=300)
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
# Names any Codex child of this tool may inherit (the worker, and ``--version`` and the schema generator through
# ``_run``: the #402 reads, round 4, DeepSeek R4-3): what it needs to find its own login and run tools.
# Every API-key name is left out, so the run uses Codex's own login (Lucas, Nov 8: "Codex's own login only").
CHILD_ENV_NAMES = ("PATH", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
                   "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR", "LANG",
                   "LC_ALL", "LC_CTYPE", "TERM", "TZ", "CODEX_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                   "XDG_STATE_HOME", "XDG_RUNTIME_DIR")
_EOF = object()
# Lines of output read at most before /stop or /quit ends the input, while the driver holds a write for the
# turn's id (round 3 of #402, GPT-6 Astra 3).
HELD_READS = 100


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
            try:
                self.proc.stdin.close()
            except OSError:
                pass  # the child is gone and its pipe with it: input has ended either way

    def read_line(self, timeout_s: float):
        from recursion_core.routes.launch import ReadTimeout

        try:
            item = self._lines.get(timeout=max(timeout_s, 0.001))
        except queue.Empty:
            raise ReadTimeout() from None
        return None if item is _EOF else item

    def fence(self, reason: str) -> None:
        """End the worker (the founder run's stand-in for the daemon's fence) and close the captures, also
        when the child is already dead (the #402 read, DeepSeek F2)."""
        try:
            self.close_stdin()
            if self.proc.poll() is None:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        finally:
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


_NOTHING_YET = object()


class _OutputFailed(Exception):
    """The worker's output was not the protocol's (the RouteError it raised is args[0]): the run ends."""


def takes_now(line: str, approval_waiting: bool) -> bool:
    """Whether a command typed while a turn runs is acted on at once (else it waits for the turn to end):
    /interrupt, /stop and /quit always; y and n when an approval waits for them; a message, as a steer, when
    an approval waits (the turn is paused on it); /resume never (it needs the stopped worker's output done)."""

    if line in ("/interrupt", "/stop", "/quit"):
        return True
    if line == "/resume":
        return False
    return approval_waiting


def session_loop(driver, commands, say, console, *, env: dict | None = None, session_id: str = "ses-attended",
                 route: dict | None = None, read_s: float = 1.0, seen: dict | None = None, on_draft=None) -> dict:
    """Run the founder's commands against ``driver``. Returns what it saw, for the capture and for tests.

    While a turn runs, the worker's output is read and, between reads, a waiting command is taken at once
    when ``takes_now`` says so, so /interrupt and /stop work without waiting for the turn (the #402 read,
    GPT-6 Astra R6); any other command waits until the turn has ended. ``commands`` is any iterator; one with a
    ``ready()`` method (the typed input of ``run_mode``) is asked before taking a command during a turn,
    so the loop never blocks on the keyboard while a turn runs. A worker whose output is not the
    protocol's ends the loop with a message; a refused or failed write is said and the loop goes on
    (DeepSeek F5). ``/resume`` is refused while the worker runs: a running worker is never replaced (R7)."""

    from recursion_core.routes import contract as c
    from recursion_core.routes.driver import DriverRefused, DriverStateError
    from recursion_core.routes.launch import ReadTimeout

    route = route or {"engine": "codex", "model": None, "connection": {"kind": "subscription", "id": "own-login"}}
    seen = seen if seen is not None else {}
    for key, empty in (("threads", []), ("drafts", []), ("workers", 0)):
        seen.setdefault(key, empty)
    state = {"handle": None, "turn": 0, "delivery": 0, "eof": False, "exhausted": False}
    source, held = iter(commands), []

    def start(esid):
        seen["workers"] += 1
        # #376's PROVISIONAL call forms (driver.py at 5079080c): start(session, workspace, env) and
        # resume(engine_session_id, session, workspace, env).
        session = {"session_id": session_id, "run_id": f"attended-{seen['workers']}", "route": route}
        if esid is None:
            state["handle"] = driver.start(session, None, env or {})
        else:
            state["handle"] = driver.resume(esid, session, None, env or {})
        state["eof"] = False
        seen["threads"].append(state["handle"].engine_session_id)
        say(f"worker {seen['workers']} on thread {state['handle'].engine_session_id}")

    def record(drafts):
        for d in drafts:
            seen["drafts"].append(d)
            if on_draft is not None:
                on_draft(d)
            say(f"{d['type']}: {json.dumps(d['payload'], sort_keys=True)}")

    def read_once() -> bool:
        """One read of the worker's output; False when there was nothing to read. Output that is not the
        protocol's raises _OutputFailed, which ends the run wherever it is read."""
        try:
            drafts = driver.pump(state["handle"], read_s)
        except ReadTimeout:
            return False
        except c.RouteError as err:
            raise _OutputFailed(err) from None
        if drafts is None:
            state["eof"] = True
            say("the worker ended its output")
            return False
        record(drafts)
        return True

    def busy() -> bool:
        handle = state["handle"]
        return not state["eof"] and (handle.live_turn is not None or bool(console.pending))

    def next_command(during_turn: bool):
        """The next command to look at, or _NOTHING_YET. Outside a turn the oldest waiting one comes first;
        during a turn the oldest waiting one that can be acted on now (``takes_now``) is taken, and otherwise a
        fresh one is read (when the source has one ready), so a waiting command never hides a later
        /interrupt, /stop or /quit, and one that must go on waiting (/resume) is not taken again and again
        (the #402 reads, round 2: GPT-6 Astra 3, GPT-6.1 Sol 2, DeepSeek R2-6; round 3: Astra 2, Sol 3)."""
        for index, waiting in enumerate(held):
            if not during_turn or takes_now(waiting.strip(), bool(console.pending)):
                return held.pop(index)
        if state["exhausted"]:
            return _NOTHING_YET
        ready = getattr(source, "ready", None)
        if during_turn and ready is not None and not ready():
            return _NOTHING_YET
        raw = next(source, None)
        if raw is None:
            state["exhausted"] = True
            return _NOTHING_YET
        return raw

    def step(line: str) -> bool:
        handle = state["handle"]
        if line in ("y", "n"):
            if console.pending:
                approval_id, _ = console.pending.pop(0)
                driver.send(handle, {"kind": "approval_answer", "approval_id": approval_id,
                                     "decision": "approve" if line == "y" else "deny"})
        elif line == "/interrupt":
            if handle.live_turn is not None:
                driver.interrupt(handle, handle.live_turn)
        elif line in ("/stop", "/quit"):
            if not handle.closed:
                # While the driver still holds a steer or an interrupt for the turn's id, the worker's output is
                # read first, so it goes out before the input ends (the #402 reads, round 2, GPT-6 Astra 3); at
                # most HELD_READS lines, so a worker that never stops writing cannot hold /stop or /quit back
                # (round 3, GPT-6 Astra 3).
                reads = 0
                while reads < HELD_READS and not state["eof"] and driver.holding(handle) and read_once():
                    reads += 1
                driver.send(handle, {"kind": "close"})
            console.pending.clear()
            return line == "/quit"
        elif line == "/resume":
            if not handle.closed:
                say("refused: /stop the worker first; a running worker is never replaced")
                return False
            start(seen["threads"][-1])
        else:
            state["delivery"] += 1
            if handle.live_turn is None:
                state["turn"] += 1
            driver.send(handle, {"kind": "user_message", "turn_id": handle.live_turn or f"turn-{state['turn']}",
                                 "text": line, "delivery_id": f"dlv-{state['delivery']}"})
        return False

    start(None)
    try:
        while True:
            during_turn = busy()
            raw = next_command(during_turn)
            if raw is _NOTHING_YET:
                if not during_turn:
                    break  # the source has ended and nothing waits (a waiting command is taken first)
                read_once()
                continue
            line = raw.strip()
            if during_turn and line and not takes_now(line, bool(console.pending)):
                held.append(raw)  # it waits for the turn's end, behind the ones already waiting
                continue
            if not line:
                continue
            try:
                if step(line):
                    break
            except (DriverRefused, DriverStateError, c.RouteError) as err:
                say(f"refused: {err}")
        handle = state["handle"]
        # What the worker still writes after /quit, while it has some, read like any other output: a line that
        # is not the protocol's ends the run with its message here too (the #402 reads, round 3, GPT-6.1 Sol 4).
        while not state["eof"] and handle.closed and read_once():
            pass
    except _OutputFailed as failed:
        say(f"the worker's output failed: {failed.args[0]}; the run ends")
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

    def on_draft(d: dict) -> None:
        drafts.write(json.dumps(d, sort_keys=True) + "\n")

    console = Console(say)
    driver = CodexDriver(launcher, time.monotonic, binary=args.codex, approvals=console)
    route = {"engine": "codex", "model": args.model, "connection": {"kind": "subscription", "id": "own-login"}}
    seen: dict = {}
    # Every worker is ended and every capture closed however the loop ends, an exception or Ctrl-C included.
    # Each step is tried even when one before it fails or is interrupted (the drafts file, a worker), and even
    # when its failure cannot be printed; the first failure is kept before it is said and raised afterwards
    # unless the loop's own exception is already on its way (the #402 reads: GPT-6 Astra R7, round 2 change 4
    # and round 3 change 4, DeepSeek F2).
    try:
        session_loop(driver, Typed(), say, console, env=child_env(dict(os.environ)), route=route, seen=seen,
                     on_draft=on_draft)
    finally:
        in_flight = sys.exc_info()[1]
        failure = None
        steps = [("the drafts file", drafts.close)]
        steps += [(f"worker pid {proc.proc.pid}", lambda proc=proc: proc.fence("attended_run_over")) for proc in workers]
        for name, step in steps:
            try:
                step()
            except BaseException as err:  # noqa: BLE001 - kept first, then said; the next step is still tried
                failure = failure or err
                try:
                    print(f"[{name} could not be ended: {type(err).__name__}: {err}]")
                except Exception:  # noqa: BLE001 - a message that cannot be printed never stops the cleanup
                    pass
        if failure is not None and in_flight is None:
            raise failure
    print(f"captured in {out}: threads {seen.get('threads')}")
    return 0


class Typed:
    """The keyboard as a command iterator: a thread reads lines (started on first use), ``ready()`` says
    whether one is waiting, and the end of input reads as ``/quit``."""

    def __init__(self) -> None:
        self._lines: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None

    def _read(self) -> None:
        while True:
            try:
                line = input("> ")
            except EOFError:
                self._lines.put("/quit")
                return
            self._lines.put(line)

    def _started(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._read, daemon=True)
            self._thread.start()

    def __iter__(self):
        return self

    def __next__(self) -> str:
        self._started()
        return self._lines.get()

    def ready(self) -> bool:
        self._started()
        return not self._lines.empty()


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
