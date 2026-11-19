"""A stdio MCP server started as a child of a run: only from the user's own entry, inside the run's cgroup,
ended by the run's Stop, and named on the Stop record (``stdio_server.py``).

The first tests need no process. The live tests start real processes and run only on Linux with a systemd
user manager, from a process inside that manager's tree (where cgroup v2 lets this uid move a process
between two of its units). Each live test makes its own transient units, in the slice the test runs in, and
stops them itself; it starts the "run" with the run's own Stop properties. Every process a test starts is in
a unit that test made, and every live test ends by checking that none of them is alive. A process the test
did not start is never signalled.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from recursion_core.control import api as a
from recursion_core.control.api import ControlAPI
from recursion_core.control.registry import EMPTY, POPULATED, InMemoryRunRegistry, JoinRefused, StaleGeneration
from recursion_core.mcp.client import stdio_server as s

CGROUP_ROOT = Path("/sys/fs/cgroup")
GUARD_S = 20.0

STUB = textwrap.dedent('''
    import json, os, signal, sys, time
    out = sys.argv[1]
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    r, w = os.pipe()
    child = os.fork()
    if child == 0:  # the grandchild: ignores SIGTERM and SIGINT too (inherited), holds no pipe of the client
        os.close(r)
        null = os.open(os.devnull, os.O_RDWR)
        os.dup2(null, 0)
        os.dup2(null, 1)
        os.close(w)
        while True:
            time.sleep(3600)
    os.close(w)
    os.read(r, 1)
    os.close(r)
    info = {"pid": os.getpid(), "child": child, "env": sorted(os.environ)}
    with open(out + ".tmp", "w") as handle:
        handle.write(json.dumps(info))
    os.replace(out + ".tmp", out)
    for raw in sys.stdin.buffer:
        msg = json.loads(raw)
        if "id" not in msg:
            continue
        method = msg.get("method")
        if method == "initialize":
            result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                      "serverInfo": {"name": "stub", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "leave", "inputSchema": {"type": "object"}}]}
        elif method == "tools/call":  # leave the run's group: write this pid into another cgroup.procs
            with open(msg["params"]["arguments"]["procs"], "w") as handle:
                handle.write(str(os.getpid()))
            result = {"content": [{"type": "text", "text": "left"}]}
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                         "error": {"code": -32601, "message": "no"}}) + "\\n")
            sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\\n")
        sys.stdout.flush()
''')

STUB_JS = textwrap.dedent('''
    const fs = require("fs"), cp = require("child_process"), rl = require("readline");
    process.on("SIGTERM", () => {}); process.on("SIGINT", () => {});
    const child = cp.spawn(process.execPath, ["-e", "process.on('SIGTERM',()=>{});process.on('SIGINT',()=>{});setInterval(()=>{},1e6)"], {stdio: "ignore"});
    fs.writeFileSync(process.argv[2], JSON.stringify({pid: process.pid, child: child.pid, env: Object.keys(process.env).sort()}));
    rl.createInterface({input: process.stdin}).on("line", (line) => {
      const msg = JSON.parse(line); if (!("id" in msg)) return;
      let result = null;
      if (msg.method === "initialize") result = {protocolVersion: msg.params.protocolVersion, capabilities: {tools: {}}, serverInfo: {name: "stub", version: "1"}};
      else if (msg.method === "tools/list") result = {tools: [{name: "leave", inputSchema: {type: "object"}}]};
      process.stdout.write(JSON.stringify(result === null ? {jsonrpc: "2.0", id: msg.id, error: {code: -32601, message: "no"}} : {jsonrpc: "2.0", id: msg.id, result}) + "\\n");
    });
''')


def _alive(pid) -> bool:
    """A process table for the registry: a pid is alive while it exists and has not exited (a zombie has)."""
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text()
    except (OSError, ValueError, TypeError):
        return False
    return stat.rsplit(")", 1)[-1].split()[0] != "Z"


def _cgroup_of(pid) -> str | None:
    try:
        for line in Path(f"/proc/{int(pid)}/cgroup").read_text().splitlines():
            if line.startswith("0::"):
                return line[3:]
    except OSError:
        pass
    return None


class _Registry:
    """Records every call; any call fails the test that expects none."""

    def __init__(self) -> None:
        self.calls = []

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError(f"registry.{name} was called")
        return call


class _Rows:
    """One stored row as the registry returns it; joins are recorded."""

    def __init__(self, row: dict) -> None:
        self.row, self.joins = row, []

    def get(self, run_id: str) -> dict | None:
        return dict(self.row) if run_id == self.row["run_id"] else None

    def join_member(self, *args) -> dict:
        self.joins.append(args)
        return dict(self.row)


class ApprovalAndEnvironmentTest(unittest.TestCase):
    def test_a_project_proposal_without_the_users_approval_starts_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp, "started")
            project = Path(tmp, ".mcp.json")
            project.write_text(json.dumps({"mcpServers": {"proposed": {
                "command": sys.executable, "args": ["-c", f"open({str(marker)!r}, 'w').close()"],
                "env": {"PROPOSED_NAME": "a value from the project"}}}}), encoding="utf-8")
            proposals = s.read_project_proposals(project)
            self.assertEqual([p.name for p in proposals], ["proposed"])
            registry = _Registry()
            for proposal in proposals:
                with self.assertRaises(s.ServerStartRefused) as caught:
                    s.start_server(proposal, registry=registry, run_id="run-1", cgroup_root=tmp)
                self.assertEqual(caught.exception.code, s.SERVER_NOT_APPROVED)
            time.sleep(0.3)  # shows only that nothing happened afterwards
            self.assertFalse(marker.exists())
            self.assertEqual(registry.calls, [])
            self.assertEqual(sorted(os.listdir(tmp)), [".mcp.json"])

    def test_a_proposal_keeps_the_names_of_its_environment_never_the_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp, ".mcp.json")
            project.write_text(json.dumps({"mcpServers": {
                "one": {"command": "srv", "args": ["--x"], "env": {"A_NAME": "secret value"}},
                "remote": {"url": "https://example.invalid/mcp"}}}), encoding="utf-8")
            (proposal,) = s.read_project_proposals(project)
            self.assertEqual(proposal, s.ProposedServer("one", ("srv", "--x"), ("A_NAME",)))
            self.assertNotIn("secret value", repr(proposal))
            entry = s.ServerEntry.from_proposal(proposal)
            self.assertEqual((entry.name, entry.command, entry.env_names), ("one", ("srv", "--x"), ("A_NAME",)))

    def test_the_server_gets_only_the_names_its_entry_lists(self) -> None:
        parent = {"NAMED": "1", "PATH": "/bin", "OTHER": "2", "ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k"}
        entry = s.ServerEntry("srv", ("srv",), ("NAMED", "ABSENT"))
        self.assertEqual(s.server_env(entry, parent), {"NAMED": "1"})

    def test_an_entry_naming_a_provider_credential_is_refused(self) -> None:
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL",
                     "CLAUDE_CODE_USE_BEDROCK", "OPENAI_API_KEY", "CODEX_API_KEY", "CLAUDE_CODE_CLIENT_KEY"):
            with self.subTest(name=name), self.assertRaises(s.ServerStartRefused) as caught:
                s.ServerEntry("srv", ("srv",), ("NAMED", name))
            self.assertEqual(caught.exception.code, s.SERVER_ENV_REFUSED)
            self.assertIn(name, caught.exception.detail)

    def test_a_run_that_is_not_running_gets_no_server(self) -> None:
        entry = s.ServerEntry("srv", (sys.executable, "-c", "pass"))
        base = {"run_id": "run-1", "tenant": "t", "engine": "codex", "generation": 1, "members": []}
        rows = [dict(base, state="stop_pending", cgroup="/x/run-1", stop={"requested_at": 1.0, "checks": 1}),
                dict(base, state="stopped", cgroup="/x/run-1", stop={"requested_at": 1.0, "checks": 1}),
                dict(base, state="running", cgroup="/x/run-1",
                     stop={"requested_at": 1.0, "checks": 0, "reason": "cgroup_unknown", "requested": False}),
                dict(base, state="starting", cgroup=None, stop=None),
                dict(base, state="running", cgroup="/x/../run-1", stop=None)]
        with tempfile.TemporaryDirectory() as tmp:
            for row in rows:
                registry = _Rows(row)
                with self.subTest(row=row), self.assertRaises(s.ServerStartRefused) as caught:
                    s.start_server(entry, registry=registry, run_id="run-1", cgroup_root=tmp)
                self.assertEqual(caught.exception.code, s.RUN_NOT_RUNNING)
                self.assertEqual(registry.joins, [])
            self.assertEqual(os.listdir(tmp), [])


def _live_reason() -> str | None:
    if not sys.platform.startswith("linux"):
        return "Linux only"
    if not (shutil.which("systemd-run") and shutil.which("systemctl") and shutil.which("sleep")):
        return "no systemd-run, systemctl or sleep"
    own = _cgroup_of(os.getpid()) or ""
    if f"/user@{os.getuid()}.service/" not in own or not Path(own).parent.name.endswith(".slice"):
        return "this test process is not in a slice of its own user manager"
    try:
        ok = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    return None if ok else "no systemd user manager"


class _UnitStop:
    """The run's kill tree for these tests: the unit's own stop, then a walk of the run's cgroup."""

    def __init__(self, units: dict) -> None:
        self.units = units  # cgroup -> unit
        self.requests = []

    def request_stop(self, cgroup: str) -> None:
        self.requests.append(cgroup)
        subprocess.run(["systemctl", "--user", "stop", "--", self.units[cgroup]], check=True, timeout=60,
                       capture_output=True)

    def confirm(self, cgroup: str, timeout_s: float) -> str:
        path = CGROUP_ROOT / cgroup.lstrip("/")
        deadline = time.monotonic() + max(timeout_s, 5.0)
        while time.monotonic() < deadline:
            try:
                if "populated 0" in (path / "cgroup.events").read_text():
                    return EMPTY
            except FileNotFoundError:
                return EMPTY
            time.sleep(0.05)
        return POPULATED


@unittest.skipIf(_live_reason(), _live_reason() or "")
class LiveRunTest(unittest.TestCase):
    """Real units, real processes, the control API's own Stop."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.units = {}
        self.pids = set()
        self.servers = []
        self.addCleanup(self._none_alive)  # runs last: after every unit of this test is stopped
        self.slice = Path(_cgroup_of(os.getpid())).parent.name

    def _unit(self, purpose: str) -> str:
        from recursion_core.daemon.launcher import STOP_PROPERTIES
        unit = f"recursion-mcp-test-{purpose}-{secrets.token_hex(4)}.service"
        command = ["systemd-run", "--user", "--quiet", "--collect", f"--unit={unit}", f"--slice={self.slice}",
                   "--service-type=exec", "--property=KillMode=control-group", "--property=Delegate=yes",
                   "--property=RemainAfterExit=yes", "--property=MemoryMax=512M",
                   *(f"--property={item}" for item in STOP_PROPERTIES), "--", shutil.which("sleep"), "600"]
        subprocess.run(command, check=True, timeout=60, capture_output=True)
        self.addCleanup(subprocess.run, ["systemctl", "--user", "stop", "--", unit], timeout=60, capture_output=True)
        shown = subprocess.run(["systemctl", "--user", "show", "--property=ControlGroup,MainPID", "--", unit],
                               check=True, timeout=30, capture_output=True, text=True).stdout
        props = dict(line.split("=", 1) for line in shown.splitlines() if "=" in line)
        group = props["ControlGroup"]
        self.assertTrue(group.startswith("/") and group.endswith(unit), group)
        self.units[group] = unit
        self.pids.add(int(props["MainPID"]))
        return group

    def _api(self, group: str):
        registry = InMemoryRunRegistry(processes=_alive)
        api = ControlAPI(registry, _UnitStop(self.units), lambda params: {"cgroup": group},
                         clock=time.time, new_run_id=lambda: "run-" + secrets.token_hex(4))
        run = api.call("run.start", {"tenant": "t1", "engine": "codex", "prompt": "p"})["run"]["run_id"]
        return api, registry, run

    def _start(self, registry, run, command, **options):
        server = s.start_server(s.ServerEntry("stub", command, options.pop("env_names", ())), registry=registry,
                                run_id=run, kill_timeout=1.0, **options)
        self.servers.append(server)
        self.pids.add(server.pid)
        return server

    def _info(self, path: Path) -> dict:
        deadline = time.monotonic() + GUARD_S
        while not path.exists():
            self.assertLess(time.monotonic(), deadline, "the stub never wrote its pids")
            time.sleep(0.05)
        info = json.loads(path.read_text())
        self.pids.update((info["pid"], info["child"]))
        return info

    def _none_alive(self) -> None:
        for server in self.servers:
            server.close()
        alive = sorted(pid for pid in self.pids if _alive(pid))
        for pid in alive:  # only a pid this test started and that still sits in one of its own units
            where = _cgroup_of(pid) or ""
            if any(where == group or where.startswith(group + "/") for group in self.units):
                os.kill(pid, 9)
        self.assertEqual(alive, [], "processes of this test outlived it")

    def _stop_and_check(self, api, registry, run, tree) -> dict:
        res = api.call("run.stop", {"run_id": run})
        left = sorted(pid for pid in tree if _alive(pid))
        rec = registry.get(run)
        if left:  # the Stop record must say which one is
            self.assertEqual((res["code"], res.get("reason")), (a.PENDING, "member_alive"), left)
            self.assertTrue(set(left) <= {m["pid"] for m in rec["members"]}, (left, rec["members"]))
        self.assertEqual(left, [])
        self.assertEqual((res["code"], rec["state"]), (a.STOPPED, "stopped"))
        self.assertEqual(rec["stop_attestation"]["member_ids"], ["mcp:stub"])
        return res

    def test_a_forking_server_that_ignores_sigterm_ends_with_the_runs_stop(self) -> None:
        group = self._unit("run")
        api, registry, run = self._api(group)
        out = Path(self.tmp.name, "pids.json")
        parent = dict(os.environ, NAMED_FOR_THE_SERVER="1", ANTHROPIC_API_KEY="not for a server",
                      OPENAI_API_KEY="not for a server")
        server = self._start(registry, run, (sys.executable, "-I", "-c", STUB, str(out)),
                             env_names=("NAMED_FOR_THE_SERVER",), parent_env=parent)
        server.client.open(deadline_s=GUARD_S)
        self.assertEqual([t["name"] for t in server.client.list_tools(deadline_s=GUARD_S)], ["leave"])
        info = self._info(out)
        self.assertEqual(info["pid"], server.pid)
        for pid in (info["pid"], info["child"]):  # inside the run's group, not the test's own
            self.assertTrue(_cgroup_of(pid).startswith(group + "/" + s.LEAF_PREFIX), _cgroup_of(pid))
        self.assertTrue(set(info["env"]) <= {"NAMED_FOR_THE_SERVER", "LC_CTYPE"}, info["env"])
        self.assertIn("NAMED_FOR_THE_SERVER", info["env"])
        self.assertEqual(registry.get(run)["members"], [{"member_id": "mcp:stub", "pid": server.pid}])
        self._stop_and_check(api, registry, run, (info["pid"], info["child"]))
        self.assertNotEqual(server.close().state, "terminated")

    def test_the_sh_c_launch_shape_ends_with_the_runs_stop(self) -> None:
        group = self._unit("run")
        api, registry, run = self._api(group)
        out = Path(self.tmp.name, "pids.json")
        # The command after it keeps sh as the parent of the server, as a launch line usually does.
        server = self._start(registry, run, ("sh", "-c", '"$0" "$@"; exit $?', sys.executable, "-I", "-c", STUB,
                                             str(out)), env_names=("PATH",))
        server.client.open(deadline_s=GUARD_S)
        info = self._info(out)
        self.assertNotEqual(info["pid"], server.pid)  # sh, then the server under it
        tree = (server.pid, info["pid"], info["child"])
        for pid in tree:
            self.assertTrue(_cgroup_of(pid).startswith(group + "/"), _cgroup_of(pid))
        self._stop_and_check(api, registry, run, tree)

    @unittest.skipUnless(shutil.which("uv"), "uv is not on this machine")
    def test_the_uv_launch_shape_ends_with_the_runs_stop(self) -> None:
        group = self._unit("run")
        api, registry, run = self._api(group)
        out = Path(self.tmp.name, "pids.json")
        script = Path(self.tmp.name, "stub.py")
        script.write_text(STUB, encoding="utf-8")
        server = self._start(registry, run, ("uv", "run", "--no-project", "--offline", "--python", sys.executable,
                                             str(script), str(out)), env_names=("PATH", "HOME"))
        server.client.open(deadline_s=GUARD_S)
        info = self._info(out)
        self._stop_and_check(api, registry, run, (server.pid, info["pid"], info["child"]))

    @unittest.skipUnless(shutil.which("npx") and shutil.which("node")
                         and not shutil.which("npx").startswith("/mnt/"), "no Linux npx and node on this machine")
    def test_the_npx_launch_shape_ends_with_the_runs_stop(self) -> None:
        group = self._unit("run")
        api, registry, run = self._api(group)
        out = Path(self.tmp.name, "pids.json")
        script = Path(self.tmp.name, "stub.js")
        script.write_text(STUB_JS, encoding="utf-8")
        server = self._start(registry, run, ("npx", "--yes", "node", str(script), str(out)),
                             env_names=("PATH", "HOME"))
        server.client.open(deadline_s=GUARD_S)
        info = self._info(out)
        self._stop_and_check(api, registry, run, (server.pid, info["pid"], info["child"]))

    def test_a_server_that_leaves_the_run_is_reported_and_never_booked_stopped(self) -> None:
        group = self._unit("run")
        elsewhere = self._unit("elsewhere")  # this test's own second unit: where the server goes
        api, registry, run = self._api(group)
        out = Path(self.tmp.name, "pids.json")
        server = self._start(registry, run, (sys.executable, "-I", "-c", STUB, str(out)))
        server.client.open(deadline_s=GUARD_S)
        info = self._info(out)
        server.client.call_tool("leave", {"procs": str(CGROUP_ROOT / elsewhere.lstrip("/") / "cgroup.procs")},
                                deadline_s=GUARD_S)
        self.assertEqual(_cgroup_of(server.pid), elsewhere)
        res = api.call("run.stop", {"run_id": run})
        rec = registry.get(run)
        self.assertEqual((res["code"], res["reason"]), (a.PENDING, "member_alive"))
        self.assertEqual(rec["state"], "stop_pending")
        self.assertEqual(rec["members"], [{"member_id": "mcp:stub", "pid": server.pid}])
        self.assertNotIn("terminated", json.dumps([res, rec]))
        self.assertFalse(_alive(info["child"]))  # the grandchild stayed in the run's group and ended with it
        self.assertTrue(_alive(server.pid))
        subprocess.run(["systemctl", "--user", "stop", "--", self.units[elsewhere]], check=True, timeout=60,
                       capture_output=True)
        self.assertFalse(_alive(server.pid))
        self.assertEqual(api.call("run.stop", {"run_id": run})["code"], a.STOPPED)

    def test_a_join_the_registry_refuses_leaves_no_server_running(self) -> None:
        group = self._unit("run")
        root = CGROUP_ROOT / group.lstrip("/")
        for refusal in (JoinRefused("run-refused", "stopped"), StaleGeneration("run-refused", 2, 3)):

            class Refusing(InMemoryRunRegistry):
                def join_member(self, run_id, expected_generation, member_id, pid):
                    raise refusal

            registry = Refusing(processes=_alive)
            api = ControlAPI(registry, _UnitStop(self.units), lambda params: {"cgroup": group}, clock=time.time,
                             new_run_id=lambda: "run-refused")
            api.call("run.start", {"tenant": "t1", "engine": "codex", "prompt": "p"})
            entry = s.ServerEntry("stub", (sys.executable, "-I", "-c", STUB, str(Path(self.tmp.name, "p"))))
            with self.subTest(refusal=type(refusal).__name__), self.assertRaises(s.ServerStartRefused) as caught:
                s.start_server(entry, registry=registry, run_id="run-refused", kill_timeout=1.0)
            self.assertEqual(caught.exception.code, s.RUN_NOT_RUNNING)
            procs = [p for leaf in root.glob(s.LEAF_PREFIX + "*") for p in (leaf / "cgroup.procs").read_text().split()]
            self.assertEqual(procs, [])
            self.assertEqual(list(root.glob(s.LEAF_PREFIX + "*")), [])


if __name__ == "__main__":
    unittest.main()
