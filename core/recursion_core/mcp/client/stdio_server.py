"""Starting a stdio MCP server as a child of a run, so that the run's Stop ends it (Linux).

A server the core starts for a run belongs to that run: it runs inside the run's cleanup group, and Stop
ends it with the run. On Linux that group is the run's cgroup (the run's transient ``systemd-run --user``
unit with ``KillMode=control-group``, ``daemon/launcher.py``). Nothing here changes how a run is launched or
stopped; this module only places a server in a run that is already running, and names it on the run's record.

The user's own entry
    Only a ``ServerEntry`` starts a server. A project's file may propose one (``read_project_proposals``
    reads the ``mcpServers`` object of a project's ``.mcp.json``); a proposal is data and is never started.
    The user's approval makes it an entry (``ServerEntry.from_proposal``); anything that is not a
    ``ServerEntry`` is refused (``SERVER_NOT_APPROVED``) before any directory, process or record exists.
The child's environment
    Only the variables the entry names, each with the value the parent has (a name the parent lacks is left
    out). A provider credential or route name of an engine (the scrub, sign-in and route names of the
    child-environment rule's engine profiles) is refused by name (``SERVER_ENV_REFUSED``), so it never
    reaches a server, whatever the entry says. A project's proposal carries names only: its values are dropped.
    One exception, not chosen here: the cgroup launcher's trampoline is a Python interpreter, and with no
    locale named its C-locale coercion (PEP 538) adds ``LC_CTYPE``.
Placement
    The server is started by the cgroup leaf launcher (``recursion_core.cgroup_leaf``) in a new leaf inside
    the run's cgroup: its trampoline joins the leaf before it runs the server, so no code of the server ever
    runs outside the run's group, and every process it starts is inside it too. It is a child of the run,
    never of the daemon's own unit. The server's pid then joins the run's record as a member
    (``member_id`` ``mcp:<name>``, the registry's ``join_member``). A run that is not running, has a Stop
    asked, or names no cgroup is refused (``RUN_NOT_RUNNING``); a name already on the run's record, its
    server alive or closed, is refused (``SERVER_NAME_TAKEN``) before any process is started; a join the
    registry refuses (the run was stopped, or its record moved since it was read) ends the leaf at once and
    is refused the same way as a run that is not running.
Stop
    The run's own Stop ends the server: the unit's stop reaches every process of its cgroup, the leaf
    included. The Stop record lists the server: a ``stopped`` answer's attestation names ``mcp:<name>`` in
    ``member_ids``, and the program the core started (the one pid on the record), still alive after its
    cgroup reads empty (it left the group), keeps the Stop ``pending`` with reason ``member_alive`` (the
    control API's own rule): it is reported and never booked as stopped or terminated. A process below that
    program (a server run by ``sh -c``, ``uv`` or ``npx``) that leaves the group is not on the record, and
    Stop does not see it.
The connection
    ``StdioServer.client`` is the MCP client of ``session.py`` over the server's stdout and stdin, under that
    client's bounds. ``StdioServer.close`` shuts the client and kills the leaf; its outcome is the leaf
    launcher's, which never says ``terminated``. It never waits on a pipe a client thread is still inside:
    after the kill it waits at most ``STREAM_WAIT_S`` for the client's reader and writer to come out of the
    server's pipes, and a pipe one of them is still inside (something outside the run's group holds its
    other end) is left open, named in ``StdioServer.streams_left`` and handed on as one record
    (``STREAMS_LEFT_RECORD``) through ``on_record``. That is reported, never acted on: no process is looked
    for or signalled by its pipe. A leaf the run's Stop has already removed closes at once
    (``leaf_empty``, ``StdioServer.leaf_already_gone``), once the started program's exit is seen.

Linux only. Windows (the run's job object) and macOS (the run's process group) are not built here and
refuse with ``PlatformUnsupported`` from the leaf launcher.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from recursion_core import cgroup_leaf
from recursion_core.child_env.policy import CLAUDE_TLS_NAMES, ENGINE_PROFILES
from recursion_core.control.registry import JoinRefused, StaleGeneration

from .session import McpClient

SERVER_NOT_APPROVED = "mcp_server_not_approved"
SERVER_ENV_REFUSED = "mcp_server_env_refused"
SERVER_START_FAILED = "mcp_server_start_failed"
RUN_NOT_RUNNING = "mcp_server_run_not_running"
SERVER_NAME_TAKEN = "mcp_server_name_taken"

# The record ``StdioServer.close`` hands to ``on_record`` when it leaves a pipe open.
STREAMS_LEFT_RECORD = "mcp_server_streams_left"

# How long ``StdioServer.close`` waits, after the leaf is killed, for the client's threads to come out of the
# server's pipes. A first value, not measured.
STREAM_WAIT_S = 2.0

MEMBER_PREFIX = "mcp:"
LEAF_PREFIX = "run-mcp-"
_NAME = re.compile(r"[A-Za-z0-9_-]{1,48}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")


def _provider_names() -> frozenset[str]:
    names = set(CLAUDE_TLS_NAMES)
    for profile in ENGINE_PROFILES.values():
        names.update(profile.scrub, profile.passthrough, profile.provider_switches)
        names.update(name for name in (profile.key_name, profile.base_url_name) if name)
    return frozenset(names)


# A server never gets these, whatever its entry names: the engines' provider credentials and route selectors.
PROVIDER_NAMES = _provider_names()


class ServerStartRefused(Exception):
    """Typed refusal; nothing of the server is left running."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def _command(value: Any) -> tuple[str, ...]:
    if (not isinstance(value, (list, tuple)) or not value
            or not all(isinstance(item, str) and item and "\0" not in item for item in value)):
        raise ServerStartRefused(SERVER_START_FAILED, "command")
    return tuple(value)


def _env_names(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(n, str) and _ENV_NAME.fullmatch(n) for n in value):
        raise ServerStartRefused(SERVER_ENV_REFUSED, "env_names")
    refused = sorted(name for name in value if name in PROVIDER_NAMES)
    if refused:
        raise ServerStartRefused(SERVER_ENV_REFUSED, f"{refused[0]}: provider_credential")
    return tuple(dict.fromkeys(value))


@dataclass(frozen=True)
class ProposedServer:
    """A server a project's file proposes. Data only: it is never started."""

    name: str
    command: tuple[str, ...]
    env_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class ServerEntry:
    """The user's own entry: the only thing that starts a server."""

    name: str
    command: tuple[str, ...]
    env_names: tuple[str, ...] = ()
    cwd: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME.fullmatch(self.name):
            raise ServerStartRefused(SERVER_START_FAILED, "name")
        object.__setattr__(self, "command", _command(self.command))
        object.__setattr__(self, "env_names", _env_names(self.env_names))

    @classmethod
    def from_proposal(cls, proposal: ProposedServer, *, cwd: str | None = None) -> ServerEntry:
        """The user's approval of a project's proposal."""
        return cls(proposal.name, proposal.command, proposal.env_names, cwd)


def read_project_proposals(path: str | os.PathLike) -> list[ProposedServer]:
    """The stdio servers a project's ``.mcp.json`` proposes (``mcpServers``: ``command``, ``args``, ``env``).

    Only the names of ``env`` are kept, never its values. A server that is not a stdio command is skipped.
    """
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)
    servers = doc.get("mcpServers") if isinstance(doc, dict) else None
    proposals = []
    for name, spec in sorted((servers or {}).items()) if isinstance(servers, dict) else ():
        if not isinstance(spec, dict) or not isinstance(spec.get("command"), str):
            continue
        args = spec.get("args") or []
        env = spec.get("env") or {}
        if not isinstance(args, list) or not isinstance(env, dict):
            continue
        proposals.append(ProposedServer(name, (spec["command"], *args), tuple(env)))
    return proposals


def server_env(entry: ServerEntry, parent: Mapping[str, str] | None = None) -> dict[str, str]:
    """The server's whole environment: only the names the entry lists, with the parent's values."""
    source = os.environ if parent is None else parent
    env = {}
    for name in _env_names(entry.env_names):
        if name in source:
            value = source[name]
            if not isinstance(value, str) or "\0" in value:
                raise ServerStartRefused(SERVER_ENV_REFUSED, f"{name}: value_malformed")
            env[name] = value
    return env


class _WatchedStream:
    """One of the server's pipes as the client uses it: it knows whether a client thread is inside it."""

    def __init__(self, stream) -> None:
        self._stream = stream
        self._cond = threading.Condition()
        self._inside = 0
        self._shut = False

    def _call(self, name: str, *args):
        with self._cond:
            if self._shut:
                raise ValueError("I/O operation on closed file.")
            self._inside += 1
        try:
            return getattr(self._stream, name)(*args)
        finally:
            with self._cond:
                self._inside -= 1
                self._cond.notify_all()

    def readline(self, size: int = -1) -> bytes:
        return self._call("readline", size)

    def write(self, data) -> int | None:
        return self._call("write", data)

    def flush(self) -> None:
        self._call("flush")

    def close(self) -> None:
        self.shut_when_idle(0.0)

    def shut_when_idle(self, deadline: float) -> bool:
        """Shut the stream once no thread is inside it, waiting until ``deadline`` (monotonic) at most.

        ``False`` when a thread is still inside then: the stream is left open, because shutting it would wait
        for that thread, and its read or write ends only when every holder of the pipe's other end is gone.
        """
        with self._cond:
            while self._inside and self._cond.wait(max(0.0, deadline - time.monotonic())):
                pass
            if self._inside:
                return False
            if not self._shut:
                self._shut = True  # no thread is inside, and none can enter now
                try:
                    self._stream.close()
                except Exception:  # noqa: BLE001 - a pipe already shut or broken
                    pass
            return True


class StdioServer:
    """One server started for one run: its leaf, its member id on the run's record, and its client."""

    def __init__(self, entry: ServerEntry, run_id: str, member_id: str, leaf: cgroup_leaf.LeafRun,
                 client: McpClient, *, streams: tuple[_WatchedStream, _WatchedStream] | None = None,
                 on_record: Callable[[dict], Any] | None = None) -> None:
        self.entry, self.run_id, self.member_id, self.leaf, self.client = entry, run_id, member_id, leaf, client
        # The server's output and input as the client was given them (``start_server`` gives both).
        self._streams = streams or (_WatchedStream(leaf.proc.stdout), _WatchedStream(leaf.proc.stdin))
        self._on_record = on_record
        # Set by ``close``: the pipes it left open ("output", "input") because a client thread was still inside
        # them after the leaf was killed: something outside the run's group still holds the server's pipe.
        self.streams_left: tuple[str, ...] = ()
        # Set by ``close``: the run's Stop had already removed the leaf, and the started program had ended.
        self.leaf_already_gone = False

    @property
    def pid(self) -> int | None:
        return self.leaf.pid

    def close(self) -> cgroup_leaf.Outcome:
        """Shut the client and kill the leaf. The outcome never says ``terminated``; see the module text."""
        try:
            self.client.close()
        finally:
            outcome = self._close_leaf()
            self._shut_streams()
        return outcome

    def _close_leaf(self) -> cgroup_leaf.Outcome:
        leaf = self.leaf
        with leaf.lock:
            # The kernel removes a cgroup only once no process is in it; the started program's own exit is seen
            # (and reaped) here, so a leaf that is missing while its program lives is never read as ended.
            gone = (not leaf.removed and leaf.proc is not None and not os.path.lexists(leaf.leaf)
                    and leaf.proc.poll() is not None)
            if gone:
                if leaf.pidfd is not None:
                    fd, leaf.pidfd = leaf.pidfd, None
                    os.close(fd)
                leaf.outcome = cgroup_leaf.Outcome("leaf_empty", leaf.proc.returncode, False, True)
                leaf.removed = True  # the leaf's own close returns this outcome from now on
        if gone:
            self.leaf_already_gone = True
        return leaf.close()

    def _shut_streams(self) -> None:
        deadline = time.monotonic() + STREAM_WAIT_S
        left = tuple(name for name, stream in zip(("output", "input"), self._streams)
                     if not stream.shut_when_idle(deadline))
        self.streams_left = left
        if left and self._on_record is not None:
            try:
                self._on_record({"record": STREAMS_LEFT_RECORD, "member_id": self.member_id, "streams": list(left)})
            except Exception:  # noqa: BLE001 - as the client's own records: a callback that raises is ignored
                pass


def _run_cgroup(registry, run_id: str, cgroup_root: Path) -> tuple[Path, dict]:
    rec = registry.get(run_id)
    if not isinstance(rec, dict):
        raise ServerStartRefused(RUN_NOT_RUNNING, "unknown")
    group = rec.get("cgroup")
    if rec.get("state") != "running" or rec.get("stop") is not None:
        raise ServerStartRefused(RUN_NOT_RUNNING, str(rec.get("state")))
    if not isinstance(group, str) or not group.startswith("/") or ".." in Path(group).parts:
        raise ServerStartRefused(RUN_NOT_RUNNING, "cgroup")
    return Path(cgroup_root) / group.lstrip("/"), rec


def start_server(entry: ServerEntry, *, registry, run_id: str, cgroup_root: str | os.PathLike = "/sys/fs/cgroup",
                 parent_env: Mapping[str, str] | None = None, on_record: Callable[[dict], Any] | None = None,
                 start_timeout: float = 30.0, kill_timeout: float = 5.0) -> StdioServer:
    """Start ``entry`` inside the run's cgroup, name it on the run's record, and connect the client.

    The session is not opened here: call ``server.client.open()``. Every refusal is ``ServerStartRefused``
    with nothing left running; a refusal after the launch kills the leaf first.
    """
    if not isinstance(entry, ServerEntry):
        raise ServerStartRefused(SERVER_NOT_APPROVED, type(entry).__name__)
    env = server_env(entry, parent_env)
    group, rec = _run_cgroup(registry, run_id, Path(cgroup_root))
    member_id = MEMBER_PREFIX + entry.name
    if any(isinstance(m, dict) and m.get("member_id") == member_id for m in rec.get("members") or ()):
        raise ServerStartRefused(SERVER_NAME_TAKEN, member_id)  # alive or closed: never started again here
    try:
        root = cgroup_leaf.DelegatedRoot(group)
        leaf = root.launch(f"{LEAF_PREFIX}{entry.name}-{secrets.token_hex(4)}", list(entry.command), env=env,
                           cwd=entry.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, start_timeout=start_timeout, kill_timeout=kill_timeout)
    except cgroup_leaf.PlatformUnsupported:
        raise
    except (cgroup_leaf.LaunchError, OSError, ValueError) as exc:
        raise ServerStartRefused(SERVER_START_FAILED, type(exc).__name__) from exc
    try:
        # The generation read above: if the record moved since (a Stop asked, another member), the join is
        # refused and the server ends here, rather than join a record this start never saw.
        registry.join_member(run_id, rec.get("generation"), member_id, leaf.pid)
    except BaseException as exc:
        try:
            leaf.close()  # the server is not on the record: it must not outlive this refusal
        finally:
            for stream in (leaf.proc.stdout, leaf.proc.stdin):
                stream.close()
        if isinstance(exc, (JoinRefused, StaleGeneration)):
            raise ServerStartRefused(RUN_NOT_RUNNING, type(exc).__name__) from exc
        raise
    streams = (_WatchedStream(leaf.proc.stdout), _WatchedStream(leaf.proc.stdin))
    client = McpClient(*streams, **({"on_record": on_record} if on_record else {}))
    return StdioServer(entry, run_id, member_id, leaf, client, streams=streams, on_record=on_record)
