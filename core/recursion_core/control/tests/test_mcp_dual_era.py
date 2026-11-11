"""CSL-766 B5 / F56: Claude Code dual-era MCP, same run id and Stop, n>=5.

No Claude Code binary, provider, network or real credential is used. Negotiation
is the offline reading recorded in ``mcp_dual_era.py``. Live wire-capture of a
real handshake is unmeasured.
"""

from __future__ import annotations

import unittest

from recursion_core.control.mcp_dual_era import (
    CLAUDE_CODE_DOCS,
    CLAUDE_CODE_PACKAGE_VERSION,
    LEGACY_PROTOCOL,
    LIVE_WIRE_CAPTURE,
    MCP_SPEC_COMMIT,
    MCP_SPEC_FILE,
    MODERN_PROTOCOL,
    TASKS_EXTENSION,
    DualEraServer,
    Negotiation,
    _iso,
    claude_code_negotiate,
    modern_params,
    negotiation_mode,
)
from recursion_core.control import mcp_tools
from recursion_core.control.api import ControlAPI
from recursion_core.control.registry import EMPTY, POPULATED
from recursion_core.control.tests.fakes import FakeKillTree, FakeStarter, InMemoryRunRegistry, make_api, started

N = 5


class _OutageRegistry(InMemoryRunRegistry):
    """Answers until ``down`` is set, then every read and write raises (a registry outage)."""

    down = False

    def get(self, run_id: str):
        if self.down:
            raise OSError("registry")
        return super().get(run_id)

    def put(self, record: dict) -> None:
        if self.down:
            raise OSError("registry")
        super().put(record)

    def list(self):
        if self.down:
            raise OSError("registry")
        return super().list()


def _server():
    api, registry, kill, starter = make_api(FakeKillTree())
    return DualEraServer(api), api, registry, kill, starter


class SourcePinTest(unittest.TestCase):
    def test_sources_are_recorded_and_live_wire_capture_is_unmeasured(self) -> None:
        self.assertEqual(CLAUDE_CODE_PACKAGE_VERSION, "2.1.312")
        self.assertEqual(CLAUDE_CODE_DOCS, "https://code.claude.com/docs/en/mcp")
        self.assertEqual(MCP_SPEC_COMMIT, "38353251fab3bdeead03dd8283b9e66076ce1a7f")
        self.assertEqual(len(MCP_SPEC_COMMIT), 40)
        int(MCP_SPEC_COMMIT, 16)
        self.assertEqual(MCP_SPEC_FILE, "schema/2026-07-28/schema.ts")
        self.assertIsNone(LIVE_WIRE_CAPTURE)


class NegotiationTests(unittest.TestCase):
    def test_invalid_env_is_ignored(self) -> None:
        self.assertIsNone(negotiation_mode(None))
        self.assertIsNone(negotiation_mode(""))
        self.assertIsNone(negotiation_mode("modern"))
        self.assertEqual(negotiation_mode("legacy"), "legacy")
        self.assertEqual(negotiation_mode("auto"), "auto")

    def _repeat(self, check) -> None:
        results = [check() for _ in range(N)]
        self.assertEqual((N, N), (len(results), results.count(True)), results)

    def test_default_negotiates_modern_when_the_server_supports_it(self) -> None:
        def check() -> bool:
            for env in (None, "auto", "unexpected"):
                got = claude_code_negotiate(env, server_supports_modern=True)
                if got != Negotiation("modern", MODERN_PROTOCOL, "server/discover", True, negotiation_mode(env)):
                    return False
            return True

        self._repeat(check)

    def test_legacy_env_initializes_on_2025_06_18(self) -> None:
        def check() -> bool:
            got = claude_code_negotiate("legacy", server_supports_modern=True)
            return got == Negotiation("legacy", LEGACY_PROTOCOL, "initialize", False, "legacy")

        self._repeat(check)

    def test_unset_may_skip_a_cached_stdio_probe_but_auto_does_not(self) -> None:
        skipped = claude_code_negotiate(None, server_supports_modern=True, skip_cached_probe=True)
        self.assertEqual(skipped.era, "legacy")
        self.assertFalse(skipped.probed)
        forced = claude_code_negotiate("auto", server_supports_modern=True, skip_cached_probe=True)
        self.assertEqual(forced.era, "modern")
        self.assertTrue(forced.probed)

    def test_server_without_modern_falls_back_to_initialize(self) -> None:
        got = claude_code_negotiate(None, server_supports_modern=False)
        self.assertEqual(got, Negotiation("legacy", LEGACY_PROTOCOL, "initialize", True, None))


class HandshakeTests(unittest.TestCase):
    def test_server_discover_advertises_both_versions(self) -> None:
        server, *_ = _server()
        reply = server.handle({
            "jsonrpc": "2.0", "id": "discover-1", "method": "server/discover",
            "params": modern_params(),
        })
        result = reply["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertIn(MODERN_PROTOCOL, result["supportedVersions"])
        self.assertIn(LEGACY_PROTOCOL, result["supportedVersions"])
        self.assertIn(TASKS_EXTENSION, result["capabilities"]["extensions"])

    def test_initialize_answers_legacy_2025_06_18(self) -> None:
        server, *_ = _server()
        reply = server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": LEGACY_PROTOCOL, "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
        })
        self.assertEqual(reply["result"]["protocolVersion"], LEGACY_PROTOCOL)
        self.assertEqual(server.legacy_sessions, 1)

    def test_default_handshake_then_legacy_handshake_against_the_same_server(self) -> None:
        """B5: modern by default and initialize under MCP_PROTOCOL_NEGOTIATION=legacy, n>=5."""

        results = []
        for _ in range(N):
            server, *_ = _server()
            default = claude_code_negotiate(None)
            legacy = claude_code_negotiate("legacy")
            modern_reply = server.handle({
                "jsonrpc": "2.0", "id": "d", "method": default.handshake,
                "params": modern_params(),
            })
            legacy_reply = server.handle({
                "jsonrpc": "2.0", "id": "i", "method": legacy.handshake,
                "params": {"protocolVersion": legacy.protocol, "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
            })
            results.append(
                default.protocol == MODERN_PROTOCOL
                and default.handshake == "server/discover"
                and MODERN_PROTOCOL in modern_reply["result"]["supportedVersions"]
                and legacy.protocol == LEGACY_PROTOCOL
                and legacy.handshake == "initialize"
                and legacy_reply["result"]["protocolVersion"] == LEGACY_PROTOCOL
            )
        self.assertEqual((N, N), (len(results), results.count(True)), results)


class SameRunAndStopTests(unittest.TestCase):
    def _start_legacy(self, server: DualEraServer, prompt: str = "p") -> str:
        reply = server.handle({
            "jsonrpc": "2.0", "id": "ls", "method": "tools/call",
            "params": {
                "name": "recursion_run_start",
                "arguments": {"tenant": "t1", "engine": "codex", "prompt": prompt},
            },
        })
        return reply["result"]["structuredContent"]["run"]["run_id"]

    def _start_modern(self, server: DualEraServer, prompt: str = "p") -> str:
        reply = server.handle({
            "jsonrpc": "2.0", "id": "ms", "method": "tools/call",
            "params": modern_params(
                tasks=True,
                name="recursion_run_start",
                arguments={"tenant": "t1", "engine": "codex", "prompt": prompt},
            ),
        })
        result = reply["result"]
        self.assertEqual(result["resultType"], "task")
        self.assertEqual(result["status"], "working")
        self.assertLessEqual(len(result["taskId"]), 128)
        return result["taskId"]

    def _repeat(self, check) -> None:
        results = [check() for _ in range(N)]
        self.assertEqual((N, N), (len(results), results.count(True)), results)

    def test_same_run_id_from_both_eras(self) -> None:
        def check() -> bool:
            server, api, *_ = _server()
            modern_id = self._start_modern(server)
            status = server.handle({
                "jsonrpc": "2.0", "id": "st", "method": "tools/call",
                "params": {"name": "recursion_run_status", "arguments": {"run_id": modern_id}},
            })
            legacy_seen = status["result"]["structuredContent"]["run"]["run_id"]
            poll = server.handle({
                "jsonrpc": "2.0", "id": "tg", "method": "tasks/get",
                "params": modern_params(taskId=modern_id),
            })
            return legacy_seen == modern_id == poll["result"]["taskId"] == api.call(
                "run.status", {"run_id": modern_id})["run"]["run_id"]

        self._repeat(check)

    def test_legacy_start_is_visible_as_a_modern_task(self) -> None:
        def check() -> bool:
            server, *_ = _server()
            run_id = self._start_legacy(server)
            poll = server.handle({
                "jsonrpc": "2.0", "id": "tg", "method": "tasks/get",
                "params": modern_params(taskId=run_id),
            })
            return poll["result"]["taskId"] == run_id and poll["result"]["status"] == "working"

        self._repeat(check)

    def test_stop_from_legacy_after_modern_start(self) -> None:
        def check() -> bool:
            server, _, _, kill, _ = _server()
            run_id = self._start_modern(server)
            stop = server.handle({
                "jsonrpc": "2.0", "id": "stop", "method": "tools/call",
                "params": {"name": "recursion_run_stop", "arguments": {"run_id": run_id}},
            })
            poll = server.handle({
                "jsonrpc": "2.0", "id": "tg", "method": "tasks/get",
                "params": modern_params(taskId=run_id),
            })
            return (
                stop["result"]["structuredContent"]["code"] == "stopped"
                and poll["result"]["status"] == "cancelled"
                and poll["result"]["taskId"] == run_id
                and len(kill.requests) == 1
                and server.notifications == []
            )

        self._repeat(check)

    def test_stop_from_modern_after_legacy_start(self) -> None:
        def check() -> bool:
            server, _, _, kill, _ = _server()
            run_id = self._start_legacy(server)
            cancel = server.handle({
                "jsonrpc": "2.0", "id": "tc", "method": "tasks/cancel",
                "params": modern_params(taskId=run_id),
            })
            status = server.handle({
                "jsonrpc": "2.0", "id": "st", "method": "tools/call",
                "params": {"name": "recursion_run_status", "arguments": {"run_id": run_id}},
            })
            return (
                cancel["result"]["resultType"] == "complete"
                and cancel["result"]["status"] == "cancelled"
                and cancel["result"]["taskId"] == run_id
                and status["result"]["structuredContent"]["run"]["state"] == "stopped"
                and len(kill.requests) == 1
                and server.notifications == []
            )

        self._repeat(check)

    def _pending_server(self):
        api, registry, kill, starter = make_api(FakeKillTree(answers=(POPULATED,)))
        return DualEraServer(api), kill

    def _assert_pending(self, server: DualEraServer, run_id: str, reply: dict) -> bool:
        poll = server.handle({"jsonrpc": "2.0", "id": "tg", "method": "tasks/get",
                              "params": modern_params(taskId=run_id)})["result"]
        status = server.handle({
            "jsonrpc": "2.0", "id": "st", "method": "tools/call",
            "params": {"name": "recursion_run_status", "arguments": {"run_id": run_id}},
        })["result"]["structuredContent"]["run"]
        return (
            all(body["status"] == "working" and body.get("statusMessage") == "pending"
                and "result" not in body for body in (reply, poll))
            and status["state"] == "stop_pending"
            and server.notifications == []
        )

    def test_unconfirmed_stop_from_modern_reads_pending_never_cancelled(self) -> None:
        # Red-team r1 finding 2: S6 "pending" through tasks/cancel and tasks/get, from both start eras.
        def check() -> bool:
            ok = True
            for start in (self._start_modern, self._start_legacy):
                server, kill = self._pending_server()
                run_id = start(server)
                cancel = server.handle({"jsonrpc": "2.0", "id": "tc", "method": "tasks/cancel",
                                        "params": modern_params(taskId=run_id)})["result"]
                ok = ok and self._assert_pending(server, run_id, cancel) and len(kill.requests) == 1
            return ok

        self._repeat(check)

    def test_unconfirmed_stop_from_legacy_reads_pending_as_a_task(self) -> None:
        def check() -> bool:
            ok = True
            for start in (self._start_modern, self._start_legacy):
                server, kill = self._pending_server()
                run_id = start(server)
                stop = server.handle({
                    "jsonrpc": "2.0", "id": "stop", "method": "tools/call",
                    "params": {"name": "recursion_run_stop", "arguments": {"run_id": run_id}},
                })["result"]["structuredContent"]
                poll = server.handle({"jsonrpc": "2.0", "id": "tg", "method": "tasks/get",
                                      "params": modern_params(taskId=run_id)})["result"]
                ok = ok and stop["code"] == "pending" and self._assert_pending(server, run_id, poll)
            return ok

        self._repeat(check)

    def test_stop_during_launch_reads_pending_not_failed(self) -> None:
        # A launch intent (state "starting", merged from #327) is a working task; a Stop kept as intent
        # during the launch reads "pending", and the starting run never reads as "failed".
        api, registry, kill, _ = make_api(FakeKillTree())
        server = DualEraServer(api)
        registry.put({"run_id": "run-launching", "tenant": "t1", "engine": "codex", "state": "starting",
                      "cgroup": None, "started_at": 1_762_639_200.0, "stop": None, "start_key": None})
        poll = server.handle({"jsonrpc": "2.0", "id": "tg", "method": "tasks/get",
                              "params": modern_params(taskId="run-launching")})["result"]
        self.assertEqual(poll["status"], "working")
        self.assertNotIn("statusMessage", poll)
        cancel = server.handle({"jsonrpc": "2.0", "id": "tc", "method": "tasks/cancel",
                                "params": modern_params(taskId="run-launching")})["result"]
        self.assertEqual((cancel["status"], cancel["statusMessage"]), ("working", "pending"))
        self.assertEqual(kill.requests, [])
        self.assertEqual(server.notifications, [])

    def test_create_task_reads_pending_when_a_stop_lands_during_the_launch(self) -> None:
        # Red-team r2 finding 1: the CreateTaskResult itself carries S6 "pending" when a Stop lands while
        # the starter runs and the core cannot confirm it; it never reads cancelled.
        def check() -> bool:
            stops: list[dict] = []
            box: list[ControlAPI] = []

            def stop_during_launch(params: dict) -> dict:
                stops.append(box[0].call("run.stop", {"run_id": params["run_id"]}))
                return {"cgroup": f"/cg/{params['run_id']}"}

            api, registry, kill, _ = make_api(FakeKillTree(answers=(POPULATED,)), starter=stop_during_launch)
            box.append(api)
            server = DualEraServer(api)
            reply = server.handle({
                "jsonrpc": "2.0", "id": "ms", "method": "tools/call",
                "params": modern_params(
                    tasks=True,
                    name="recursion_run_start",
                    arguments={"tenant": "t1", "engine": "codex", "prompt": "p"},
                ),
            })["result"]
            run_id = reply.get("taskId")
            return (
                [s["code"] for s in stops] == ["pending"]
                and reply["resultType"] == "task"
                and reply["status"] == "working"
                and reply.get("statusMessage") == "pending"
                and registry.get(run_id)["state"] == "stop_pending"
                and kill.requests == [f"/cg/{run_id}"]
                and server.notifications == []
            )

        self._repeat(check)

    def test_create_task_without_a_stop_has_no_status_message(self) -> None:
        server, *_ = _server()
        reply = server.handle({
            "jsonrpc": "2.0", "id": "ms", "method": "tools/call",
            "params": modern_params(
                tasks=True,
                name="recursion_run_start",
                arguments={"tenant": "t1", "engine": "codex", "prompt": "p"},
            ),
        })["result"]
        self.assertEqual((reply["resultType"], reply["status"]), ("task", "working"))
        self.assertNotIn("statusMessage", reply)

    def test_modern_start_without_tasks_extension_is_a_plain_tool_result(self) -> None:
        server, *_ = _server()
        reply = server.handle({
            "jsonrpc": "2.0", "id": "ms", "method": "tools/call",
            "params": modern_params(
                tasks=False,
                name="recursion_run_start",
                arguments={"tenant": "t1", "engine": "codex", "prompt": "p"},
            ),
        })
        self.assertEqual(reply["result"]["resultType"], "complete")
        self.assertNotIn("taskId", reply["result"])
        self.assertEqual(reply["result"]["structuredContent"]["code"], "started")

    def test_list_runs_stays_a_plain_tool_and_tasks_list_is_not_served(self) -> None:
        server, *_ = _server()
        listed = server.handle({
            "jsonrpc": "2.0", "id": "l", "method": "tools/call",
            "params": modern_params(name="recursion_run_list", arguments={}),
        })
        self.assertEqual(listed["result"]["resultType"], "complete")
        refused = server.handle({"jsonrpc": "2.0", "id": "tl", "method": "tasks/list", "params": modern_params()})
        self.assertIn("error", refused)

    def test_tasks_require_auth_when_a_token_is_configured(self) -> None:
        api, *_rest = make_api(FakeKillTree())
        server = DualEraServer(api, auth_token="secret")
        start = server.handle({
            "jsonrpc": "2.0", "id": "ms", "method": "tools/call",
            "params": {
                "name": "recursion_run_start",
                "arguments": {"tenant": "t1", "engine": "codex", "prompt": "p"},
                "_meta": {"authorization": "secret"},
            },
        })
        run_id = start["result"]["structuredContent"]["run"]["run_id"]
        denied = server.handle({
            "jsonrpc": "2.0", "id": "tg", "method": "tasks/get",
            "params": modern_params(taskId=run_id),
        })
        self.assertEqual(denied["error"]["message"], "unauthorized")
        allowed = server.handle({
            "jsonrpc": "2.0", "id": "tg2", "method": "tasks/get",
            "params": modern_params(taskId=run_id, meta={"authorization": "secret"}),
        })
        self.assertEqual(allowed["result"]["taskId"], run_id)

    def test_tools_list_returns_the_control_tool_descriptors_in_both_eras(self) -> None:
        # Devin r4506001295: the handshake advertises tools, so tools/list must answer.
        server, *_ = _server()
        legacy = server.handle({"jsonrpc": "2.0", "id": "tl", "method": "tools/list", "params": {}})
        modern = server.handle({"jsonrpc": "2.0", "id": "tl2", "method": "tools/list", "params": modern_params()})
        expected = mcp_tools.descriptors()
        self.assertEqual(legacy["result"], {"tools": expected})
        self.assertEqual(modern["result"], {"resultType": "complete", "tools": expected})
        names = [t["name"] for t in expected]
        self.assertEqual(names, ["recursion_run_list", "recursion_run_status", "recursion_run_start",
                                 "recursion_run_stop"])
        stop = next(t for t in expected if t["name"] == "recursion_run_stop")
        self.assertIs(stop["annotations"]["destructiveHint"], True)

    def test_unknown_task_ids_are_a_jsonrpc_invalid_params_error(self) -> None:
        # Devin r4506001365: a typo id must not read as a real run that failed.
        server, api, registry, kill, _ = _server()
        for method in ("tasks/get", "tasks/cancel"):
            with self.subTest(method=method):
                reply = server.handle({"jsonrpc": "2.0", "id": "u", "method": method,
                                       "params": modern_params(taskId="run-typo")})
                self.assertNotIn("result", reply)
                self.assertEqual(reply["error"], {"code": -32602, "message": "run_unknown"})
        self.assertEqual(api.call("run.list", {})["runs"], [])
        self.assertEqual(kill.requests, [])

    def test_cancel_after_the_kill_reads_pending_when_the_registry_stops_answering(self) -> None:
        # Red-team r3 finding 1: the Stop was sent and is unconfirmed, then the record cannot be re-read.
        # tasks/cancel keeps run.stop's "pending" (S6) instead of calling the task unknown.
        registry = _OutageRegistry()
        api, registry, kill, _ = make_api(FakeKillTree(answers=(POPULATED,)), registry=registry)
        server = DualEraServer(api)
        run_id = server.handle({
            "jsonrpc": "2.0", "id": "s", "method": "tools/call",
            "params": {"name": "recursion_run_start", "arguments": {"tenant": "t1", "engine": "codex", "prompt": "p"}},
        })["result"]["structuredContent"]["run"]["run_id"]
        real_stop = api.stop

        def stop_then_outage(rid: str) -> dict:
            answer = real_stop(rid)
            registry.down = True
            return answer

        api.stop = stop_then_outage
        cancel = server.handle({"jsonrpc": "2.0", "id": "tc", "method": "tasks/cancel",
                                "params": modern_params(taskId=run_id)})
        self.assertEqual(kill.requests, [f"/sys/fs/cgroup/fake/{run_id}"])
        self.assertNotIn("error", cancel)
        body = cancel["result"]
        self.assertEqual((body["taskId"], body["status"], body["statusMessage"]), (run_id, "working", "pending"))
        self.assertNotIn("result", body)
        self.assertEqual(server.notifications, [])

    def _cancel(self, server, run_id: str) -> dict:
        return server.handle({"jsonrpc": "2.0", "id": "tc", "method": "tasks/cancel",
                              "params": modern_params(taskId=run_id)})

    def test_confirmed_stop_whose_write_fails_reads_cancelled_not_pending(self) -> None:
        # Bot review 4511585104 at 5b64f8d2: the first cancel left the run stop_pending; the second confirmed
        # an empty cgroup but its write-back failed, and the reply read the stale record as "pending".
        from recursion_core.control.registry import EMPTY
        registry = _OutageRegistry()
        api, registry, kill, _ = make_api(FakeKillTree(answers=(POPULATED, EMPTY)), registry=registry)
        server = DualEraServer(api)
        run_id = started(api)
        first = self._cancel(server, run_id)["result"]
        self.assertEqual((first["status"], first["statusMessage"]), ("working", "pending"))
        real_put = registry.put

        def refuse_stopped(record: dict) -> None:
            if record.get("state") == "stopped":
                raise OSError("registry")
            real_put(record)

        registry.put = refuse_stopped
        body = self._cancel(server, run_id)["result"]
        self.assertEqual(body["status"], "cancelled")
        self.assertNotEqual(body.get("statusMessage"), "pending")
        self.assertEqual(body["result"], {"content": [{"type": "text", "text": "cancelled"}]})

    def test_cancel_fallback_keeps_the_recorded_stop_time(self) -> None:
        # Bot review 4511755938 at 6f0360b0: the fallback reply replaced the stop time with the server clock,
        # so lastUpdatedAt moved backward on the next successful poll.
        from recursion_core.control.registry import EMPTY
        registry = _OutageRegistry()
        now = [1000.0]
        api = ControlAPI(registry, FakeKillTree(answers=(EMPTY,)), FakeStarter(), clock=lambda: now[0])
        server = DualEraServer(api, clock=lambda: now[0])
        run_id = started(api)
        self._cancel(server, run_id)
        recorded = registry.get(run_id)["stop"]["requested_at"]
        now[0] = 4600.0
        real_stop = api.stop

        def stop_then_outage(rid: str) -> dict:
            answer = real_stop(rid)
            registry.down = True
            return answer

        api.stop = stop_then_outage
        body = self._cancel(server, run_id)["result"]
        registry.down = False
        polled = server.handle({"jsonrpc": "2.0", "id": "g", "method": "tasks/get",
                                "params": modern_params(taskId=run_id)})["result"]
        self.assertEqual(body["lastUpdatedAt"], polled["lastUpdatedAt"])
        self.assertEqual(registry.get(run_id)["stop"]["requested_at"], recorded)

    def test_unrecorded_first_stop_does_not_invent_a_later_timestamp(self) -> None:
        # Bot review 4517669064 at 7372b527: a confirmed Stop whose write never
        # lands must not publish the server clock. The next tasks/get still
        # reads the unchanged start time, so the two lastUpdatedAt values match.
        from recursion_core.control.registry import EMPTY
        registry = _OutageRegistry()
        now = [1000.0]
        api = ControlAPI(registry, FakeKillTree(answers=(EMPTY,)), FakeStarter(), clock=lambda: now[0])
        server = DualEraServer(api, clock=lambda: now[0])
        run_id = started(api)
        real_put = registry.put

        def refuse_stop_write(record: dict) -> None:
            if record.get("stop") is not None:
                raise OSError("registry")
            real_put(record)

        registry.put = refuse_stop_write
        now[0] = 2000.0
        body = self._cancel(server, run_id)["result"]
        polled = server.handle({"jsonrpc": "2.0", "id": "g", "method": "tasks/get",
                                "params": modern_params(taskId=run_id)})["result"]
        self.assertEqual(body["status"], "cancelled")
        self.assertEqual(body["lastUpdatedAt"], "1970-01-01T00:16:40Z")
        self.assertEqual(body["lastUpdatedAt"], polled["lastUpdatedAt"])
        self.assertNotEqual(body["lastUpdatedAt"], "1970-01-01T00:33:20Z")
        stored = registry.get(run_id)
        self.assertIsNone(stored.get("stop"))
        self.assertNotEqual(stored.get("state"), "stopped")

    def test_stop_intent_on_a_failed_start_stays_working_pending(self) -> None:
        # Same rule as the TUI and CLI: a Stop kept as intent on a start_failed run is unconfirmed, so the
        # task is working + "pending", never the terminal "failed".
        registry = InMemoryRunRegistry()
        api, registry, _, _ = make_api(registry=registry)
        registry.put({"run_id": "run-x", "tenant": "t1", "engine": "codex", "state": "start_failed",
                      "cgroup": None, "started_at": 1000.0, "start_key": None,
                      "stop": {"requested_at": 1000.0, "checks": 1, "reason": "cgroup_unknown", "requested": False}})
        server = DualEraServer(api)
        body = server.handle({"jsonrpc": "2.0", "id": "g", "method": "tasks/get",
                              "params": modern_params(taskId="run-x")})["result"]
        self.assertEqual((body["status"], body["statusMessage"]), ("working", "pending"))

    def test_registry_outage_on_tasks_is_an_internal_error_not_an_unknown_task(self) -> None:
        registry = _OutageRegistry()
        api, registry, kill, _ = make_api(FakeKillTree(), registry=registry)
        server = DualEraServer(api)
        run_id = started(api)
        registry.down = True
        for method in ("tasks/get", "tasks/cancel"):
            with self.subTest(method=method):
                reply = server.handle({"jsonrpc": "2.0", "id": "o", "method": method,
                                       "params": modern_params(taskId=run_id)})
                self.assertNotIn("result", reply)
                self.assertEqual(reply["error"], {"code": -32603, "message": "registry_unavailable"})
        self.assertEqual(kill.requests, [])  # nothing is sent to the Stop path while the record is unreadable

    def test_task_timestamps_come_from_the_run_record_not_1970(self) -> None:
        # Devin r4506001424: creation reply and later polls agree on the run's real start time.
        registry, kill, starter = InMemoryRunRegistry(), FakeKillTree(), FakeStarter()
        now = 1_762_639_200.0  # 2025-11-08T22:00:00Z
        api = ControlAPI(registry, kill, starter, clock=lambda: now)
        server = DualEraServer(api)
        created = server.handle({
            "jsonrpc": "2.0", "id": "ms", "method": "tools/call",
            "params": modern_params(tasks=True, name="recursion_run_start",
                                    arguments={"tenant": "t1", "engine": "codex", "prompt": "p"}),
        })["result"]
        self.assertEqual(created["resultType"], "task")
        self.assertEqual(created["createdAt"], "2025-11-08T22:00:00Z")
        polled = server.handle({"jsonrpc": "2.0", "id": "g", "method": "tasks/get",
                                "params": modern_params(taskId=created["taskId"])})["result"]
        self.assertEqual(polled["createdAt"], created["createdAt"])
        self.assertFalse(DualEraServer(api)._clock() < 1_000_000_000.0, "default clock must be wall time")

    def test_tool_calls_require_auth_when_a_token_is_configured(self) -> None:
        # Devin r4506001468: tools/call and tools/list never reach ControlAPI without the token.
        api, registry, *_ = make_api(FakeKillTree())
        server = DualEraServer(api, auth_token="secret")
        calls = []
        real = api.call
        api.call = lambda method, params: calls.append(method) or real(method, params)
        start = {"name": "recursion_run_start", "arguments": {"tenant": "t1", "engine": "codex", "prompt": "p"}}
        for label, params in (("legacy", dict(start)), ("modern", modern_params(tasks=True, **start)),
                              ("wrong", dict(start, _meta={"authorization": "nope"}))):
            for method in ("tools/call", "tools/list"):
                with self.subTest(era=label, method=method):
                    denied = server.handle({"jsonrpc": "2.0", "id": "x", "method": method, "params": params})
                    self.assertEqual(denied["error"], {"code": -32001, "message": "unauthorized"})
        self.assertEqual(calls, [])
        ok = server.handle({"jsonrpc": "2.0", "id": "y", "method": "tools/call",
                            "params": dict(start, _meta={"authorization": "secret"})})
        self.assertEqual(ok["result"]["structuredContent"]["code"], "started")
        top = server.handle({"jsonrpc": "2.0", "id": "z", "method": "tools/list",
                             "params": {"authorization": "secret"}})
        self.assertEqual(top["result"]["tools"][0]["name"], "recursion_run_list")
        weird = server.handle({"jsonrpc": "2.0", "id": "w", "method": "tools/list",
                               "params": {"authorization": 1}})
        self.assertEqual(weird["error"], {"code": -32001, "message": "unauthorized"})
        self.assertEqual(calls, ["run.start"])

    def test_ungguessable_length_and_whitespace_ids_are_rejected(self) -> None:
        server, *_ = _server()
        bad = server.handle({
            "jsonrpc": "2.0", "id": "tg", "method": "tasks/get",
            "params": modern_params(taskId="has space"),
        })
        self.assertIn("error", bad)
        too_long = server.handle({
            "jsonrpc": "2.0", "id": "tg2", "method": "tasks/cancel",
            "params": modern_params(taskId="a" * 129),
        })
        self.assertIn("error", too_long)


class NotificationAndTimestampTests(unittest.TestCase):
    def _record(self, **overrides) -> dict:
        rec = {
            "run_id": "run-1", "tenant": "t1", "engine": "codex", "state": "running",
            "cgroup": "/cg/run-1", "started_at": 1_762_639_200.0, "stop": None, "start_key": None,
        }
        rec.update(overrides)
        return rec

    def test_inbound_notification_is_not_a_response_and_does_not_stop_a_run(self) -> None:
        api, registry, kill, _ = make_api(FakeKillTree([EMPTY]))
        server = DualEraServer(api)
        run = started(api)
        reply = server.handle({
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 0, "reason": "stop"},
        })
        self.assertIsNone(reply)
        self.assertEqual(registry.get(run)["state"], "running")
        self.assertEqual(kill.requests, [])
        self.assertEqual(server.notifications, [{
            "method": "notifications/cancelled",
            "params": {"requestId": 0, "reason": "stop"},
        }])
        other = server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertIsNone(other)
        self.assertEqual(registry.get(run)["state"], "running")

    def test_request_id_zero_is_answered(self) -> None:
        server, *_ = _server()
        reply = server.handle({"jsonrpc": "2.0", "id": 0, "method": "tools/list", "params": {}})
        self.assertEqual(reply["id"], 0)
        self.assertIn("tools", reply["result"])
        null_id = server.handle({"jsonrpc": "2.0", "id": None, "method": "tools/list", "params": {}})
        self.assertIsInstance(null_id, dict)
        self.assertIn("id", null_id)
        self.assertIsNone(null_id["id"])

    def test_started_at_zero_stays_the_epoch_when_stop_time_is_not_numeric(self) -> None:
        clock = 1_762_639_200.0
        api, registry, _, _ = make_api()
        registry.put(self._record(started_at=0, stop={"requested_at": "nope"}))
        server = DualEraServer(api, clock=lambda: clock)
        poll = server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tasks/get",
            "params": modern_params(taskId="run-1"),
        })["result"]
        self.assertEqual(poll["createdAt"], "1970-01-01T00:00:00Z")
        self.assertEqual(poll["lastUpdatedAt"], "1970-01-01T00:00:00Z")

    def test_non_numeric_started_at_does_not_escape_tasks_get(self) -> None:
        clock = 1_762_639_200.0
        api, registry, _, _ = make_api()
        registry.put(self._record(started_at="yesterday"))
        server = DualEraServer(api, clock=lambda: clock)
        reply = server.handle({
            "jsonrpc": "2.0", "id": 2, "method": "tasks/get",
            "params": modern_params(taskId="run-1"),
        })
        self.assertEqual(reply["result"]["createdAt"], _iso(clock))
        self.assertEqual(reply["result"]["lastUpdatedAt"], _iso(clock))

    def test_bool_started_at_is_not_a_timestamp(self) -> None:
        clock = 1_762_639_200.0
        api, registry, _, _ = make_api()
        registry.put(self._record(started_at=True, stop={"requested_at": True}))
        server = DualEraServer(api, clock=lambda: clock)
        poll = server.handle({
            "jsonrpc": "2.0", "id": 3, "method": "tasks/get",
            "params": modern_params(taskId="run-1"),
        })["result"]
        self.assertEqual(poll["createdAt"], _iso(clock))
        self.assertEqual(poll["lastUpdatedAt"], _iso(clock))
        created = server._create_task({
            "structuredContent": {
                "code": "started",
                "run": {"run_id": "missing", "state": "running", "started_at": True},
            }
        })
        self.assertEqual(created["createdAt"], _iso(clock))
        epoch = server._create_task({
            "structuredContent": {
                "code": "started",
                "run": {"run_id": "missing", "state": "running", "started_at": 0},
            }
        })
        self.assertEqual(epoch["createdAt"], "1970-01-01T00:00:00Z")

    def test_non_finite_started_at_does_not_escape_tasks_get(self) -> None:
        clock = 1_762_639_200.0
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                api, registry, _, _ = make_api()
                registry.put(self._record(started_at=value))
                server = DualEraServer(api, clock=lambda: clock)
                reply = server.handle({
                    "jsonrpc": "2.0", "id": 4, "method": "tasks/get",
                    "params": modern_params(taskId="run-1"),
                })
                self.assertEqual(reply["result"]["createdAt"], _iso(clock))
                self.assertEqual(reply["result"]["lastUpdatedAt"], _iso(clock))

    def test_oversized_int_timestamps_do_not_escape(self) -> None:
        # float(10**400) raises OverflowError before fromtimestamp. JSON can carry that integer.
        clock = 1_762_639_200.0
        started = 1000.0
        for value in (10**400, -(10**400)):
            with self.subTest(value=value):
                api, registry, _, _ = make_api()
                registry.put(self._record(started_at=value))
                server = DualEraServer(api, clock=lambda: clock)
                reply = server.handle({
                    "jsonrpc": "2.0", "id": 5, "method": "tasks/get",
                    "params": modern_params(taskId="run-1"),
                })
                self.assertEqual(reply["result"]["createdAt"], _iso(clock))
                self.assertEqual(reply["result"]["lastUpdatedAt"], _iso(clock))
                created = server._create_task({
                    "structuredContent": {
                        "code": "started",
                        "run": {"run_id": "missing", "state": "running", "started_at": value},
                    }
                })
                self.assertEqual(created["createdAt"], _iso(clock))
                self.assertEqual(created["lastUpdatedAt"], _iso(clock))
                registry.put(self._record(started_at=started, stop={"requested_at": value}))
                poll = server.handle({
                    "jsonrpc": "2.0", "id": 6, "method": "tasks/get",
                    "params": modern_params(taskId="run-1"),
                })["result"]
                self.assertEqual(poll["createdAt"], _iso(started))
                self.assertEqual(poll["lastUpdatedAt"], _iso(started))

    def test_cancel_fallback_keeps_a_bool_stop_time_aligned_with_tasks_get(self) -> None:
        # An existing bool requested_at is not a time, but replacing it with the server clock
        # in this reply makes the next tasks/get look older: the stored value is still the bool,
        # and _task_body then falls back to started_at.
        from recursion_core.control.registry import EMPTY
        registry = _OutageRegistry()
        api = ControlAPI(registry, FakeKillTree(answers=(EMPTY,)), FakeStarter(), clock=lambda: 1000.0)
        server = DualEraServer(api, clock=lambda: 2000.0)
        run_id = started(api)
        rec = registry.get(run_id)
        rec["stop"] = {"requested_at": True, "checks": 1, "reason": "populated", "requested": True}
        registry.put(rec)
        real_stop = api.stop

        def stop_then_outage(rid: str) -> dict:
            answer = real_stop(rid)
            registry.down = True
            return answer

        api.stop = stop_then_outage
        body = server.handle({
            "jsonrpc": "2.0", "id": "tc", "method": "tasks/cancel",
            "params": modern_params(taskId=run_id),
        })["result"]
        registry.down = False
        polled = server.handle({
            "jsonrpc": "2.0", "id": "g", "method": "tasks/get",
            "params": modern_params(taskId=run_id),
        })["result"]
        self.assertEqual(body["lastUpdatedAt"], _iso(1000.0))
        self.assertEqual(body["lastUpdatedAt"], polled["lastUpdatedAt"])
        self.assertNotEqual(body["lastUpdatedAt"], _iso(2000.0))
        self.assertIs(registry.get(run_id)["stop"]["requested_at"], True)

    def test_cancel_fallback_leaves_a_missing_stop_time_absent(self) -> None:
        # A stop record that already exists but has no requested_at must not gain the
        # server clock. The stored record stays without that field, and tasks/get
        # falls back to started_at, so the two replies have to match.
        from recursion_core.control.registry import EMPTY
        registry = _OutageRegistry()
        api = ControlAPI(registry, FakeKillTree(answers=(EMPTY,)), FakeStarter(), clock=lambda: 1000.0)
        server = DualEraServer(api, clock=lambda: 2000.0)
        run_id = started(api)
        rec = registry.get(run_id)
        rec["stop"] = {"checks": 1, "requested": True}
        registry.put(rec)
        real_stop = api.stop

        def stop_then_outage(rid: str) -> dict:
            answer = real_stop(rid)
            registry.down = True
            return answer

        api.stop = stop_then_outage
        body = server.handle({
            "jsonrpc": "2.0", "id": "tc", "method": "tasks/cancel",
            "params": modern_params(taskId=run_id),
        })["result"]
        registry.down = False
        polled = server.handle({
            "jsonrpc": "2.0", "id": "g", "method": "tasks/get",
            "params": modern_params(taskId=run_id),
        })["result"]
        stored = registry.get(run_id)["stop"]
        self.assertNotIn("requested_at", stored)
        self.assertEqual(body["lastUpdatedAt"], _iso(1000.0))
        self.assertEqual(body["lastUpdatedAt"], polled["lastUpdatedAt"])
        self.assertNotEqual(body["lastUpdatedAt"], _iso(2000.0))


if __name__ == "__main__":
    unittest.main()
