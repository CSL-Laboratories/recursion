#!/usr/bin/env python3
"""Control API over loopback HTTP: auth negatives, request hygiene, both engines, CLI.

Provider-free. Servers bind 127.0.0.1 on an ephemeral port. The one real
subprocess is this package's own ``serve-fake`` entry point.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import io
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock as mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v3.control_api import errors as err  # noqa: E402
from scripts.v3.control_api.auth import SCOPE_CONTROL, SCOPE_OBSERVE  # noqa: E402
from scripts.v3.control_api.client import (  # noqa: E402
    ControlClient,
    ControlRpcError,
    TokenFileRefused,
    read_token_file,
    write_token_file,
)
from scripts.v3.control_api.fake_engines import ENGINES, build_fake_stack  # noqa: E402
from scripts.v3.control_api.http_server import ControlHTTPServer, RemoteBindRefused  # noqa: E402

PROMPT = "Review the change and answer with a verdict."

# Windows does not preserve POSIX owner/group/other bits, and read_token_file
# skips that check when os.name == "nt". Creating a symlink there also needs
# privileges this suite does not have. Both checks stay enforced on POSIX.
_SKIP_POSIX_TOKEN_MODE = (
    "POSIX permission modes are not meaningful on Windows; "
    "read_token_file skips the mode check when os.name == 'nt'"
)
_SKIP_TOKEN_SYMLINK = (
    "symbolic links need extra privileges on Windows and are unsupported for this token-file check"
)
_SKIP_SIGTERM_STOP = (
    "Windows terminate() uses TerminateProcess and does not deliver a catchable SIGTERM"
)
_SKIP_POSIX_UNLINK_WHILE_OPEN = (
    "POSIX shutdown unlinks the token file while its descriptor stays open; "
    "Windows has to close that descriptor first"
)


class Live:
    """A running fake stack with tokens for two tenants and a viewer."""

    def __init__(self) -> None:
        self.stack = build_fake_stack()
        self.server = ControlHTTPServer(self.stack.service).start()
        self.control = self.stack.issue("op-a", "tenant-a", {SCOPE_CONTROL, SCOPE_OBSERVE})
        self.viewer = self.stack.issue("viewer-a", "tenant-a", {SCOPE_OBSERVE})
        self.other = self.stack.issue("op-b", "tenant-b", {SCOPE_CONTROL, SCOPE_OBSERVE})

    def client(self, token: str | None) -> ControlClient:
        return ControlClient(self.server.url, token)

    def close(self) -> None:
        self.server.stop()


class HttpTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.live = Live()
        self.addCleanup(self.live.close)

    def raw(self, method: str, path: str, *, body: bytes | None = None, headers: dict | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.live.server.port, timeout=10)
        try:
            sent = {"Host": f"127.0.0.1:{self.live.server.port}"}
            sent.update(headers or {})
            connection.request(method, path, body=body, headers=sent)
            response = connection.getresponse()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
        finally:
            connection.close()

    def rpc_bytes(self, method: str = "service.info") -> bytes:
        return json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}).encode()


class BothEnginesOverHttpTests(HttpTestCase):
    def test_codex_and_claude_runs_complete_and_are_observable(self) -> None:
        client = self.live.client(self.live.control)
        for engine in ENGINES:
            client.call("session.open", {"engine": engine, "session": f"{engine}-one"})
            out = client.call("session.run", {"engine": engine, "session": f"{engine}-one", "prompt": PROMPT})
            self.assertEqual("ok", out["result"]["status"], engine)
            self.assertEqual(engine, out["request"]["engine_id"])
        page = client.poll(0)
        kinds = [(e["engine"], e["kind"]) for e in page["events"]]
        self.assertEqual(
            [("claude", "session.opened"), ("claude", "run.started"), ("claude", "run.finished"),
             ("codex", "session.opened"), ("codex", "run.started"), ("codex", "run.finished")],
            sorted(kinds, key=lambda item: (item[0], ["session.opened", "run.started", "run.finished"].index(item[1]))),
        )
        self.assertFalse(page["reset"])

    def test_long_poll_wakes_on_a_new_event(self) -> None:
        client = self.live.client(self.live.control)
        head = client.poll(0)["next_after"]
        out: list[dict] = []
        thread = threading.Thread(target=lambda: out.append(self.live.client(self.live.control).poll(head, wait_s=10)))
        thread.start()
        time.sleep(0.2)
        client.call("session.open", {"engine": "claude", "session": "s-one"})
        thread.join(10)
        self.assertEqual(["session.opened"], [e["kind"] for e in out[0]["events"]])

    def test_tenants_are_isolated_over_http(self) -> None:
        a, b = self.live.client(self.live.control), self.live.client(self.live.other)
        a.call("session.open", {"engine": "codex", "session": "shared"})
        with self.assertRaises(ControlRpcError) as raised:
            b.call("session.get", {"engine": "codex", "session": "shared"})
        self.assertEqual(err.UNKNOWN_SESSION, raised.exception.code)
        self.assertEqual([], b.poll(0)["events"])


class AuthNegativeTests(HttpTestCase):
    def post(self, token_header: str | None, body: bytes | None = None):
        headers = {"Content-Type": "application/json"}
        if token_header is not None:
            headers["Authorization"] = token_header
        return self.raw("POST", "/v1/rpc", body=body or self.rpc_bytes(), headers=headers)

    def test_missing_malformed_and_wrong_credentials_are_401(self) -> None:
        wrong = "x" * 43
        for header in (None, "", "Bearer", "Basic dXNlcjpwYXNz", f"Bearer {wrong}", f"Token {self.live.control}"):
            status, headers, body = self.post(header)
            self.assertEqual(401, status, header)
            self.assertEqual("Bearer", headers["www-authenticate"])
            self.assertEqual(err.UNAUTHENTICATED, json.loads(body)["error"]["code"])
            self.assertNotIn(self.live.control, body.decode())

    def test_a_token_in_the_url_or_body_is_not_a_credential(self) -> None:
        token = self.live.control
        status, _, _ = self.raw("GET", f"/v1/events?after=0&token={token}")
        self.assertEqual(400, status)
        status, _, _ = self.raw("GET", "/v1/events?after=0", headers={"X-Api-Key": token, "Cookie": f"t={token}"})
        self.assertEqual(401, status)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "service.info", "params": {"token": token}}).encode()
        status, _, _ = self.raw("POST", "/v1/rpc", body=body, headers={"Content-Type": "application/json"})
        self.assertEqual(401, status)

    def test_unauthenticated_callers_learn_nothing_about_methods(self) -> None:
        results = {
            self.post(None, body)[2]
            for body in (self.rpc_bytes("service.info"), self.rpc_bytes("admin.shell"), b"{garbage")
        }
        self.assertEqual(1, len(results))

    def test_a_viewer_token_cannot_control_over_http(self) -> None:
        viewer = self.live.client(self.live.viewer)
        for method, params in (
            ("session.open", {"engine": "claude", "session": "s-one"}),
            ("session.run", {"engine": "claude", "session": "s-one", "prompt": PROMPT}),
        ):
            status, response = viewer.exchange(method, params)
            self.assertEqual(403, status)
            self.assertEqual(err.FORBIDDEN, response["error"]["code"])
        self.assertEqual(200, viewer.exchange("service.info")[0])

    def test_revoked_token_stops_working_immediately(self) -> None:
        client = self.live.client(self.live.control)
        self.assertEqual("tenant-a", client.call("service.info")["tenant"])
        self.live.stack.tokens.revoke("op-a")
        status, _ = client.exchange("service.info")
        self.assertEqual(401, status)


class RequestHygieneTests(HttpTestCase):
    def auth(self, **extra) -> dict:
        return {"Authorization": f"Bearer {self.live.control}", "Content-Type": "application/json", **extra}

    def test_host_header_must_be_the_loopback_authority(self) -> None:
        for host in ("evil.example", "evil.example:80", "127.0.0.1", f"127.0.0.1.evil.example:{self.live.server.port}", "0.0.0.0:1"):
            status, _, _ = self.raw("POST", "/v1/rpc", body=self.rpc_bytes(), headers=self.auth(Host=host))
            self.assertEqual(403, status, host)
        status, _, _ = self.raw("POST", "/v1/rpc", body=self.rpc_bytes(), headers=self.auth(Host=f"localhost:{self.live.server.port}"))
        self.assertEqual(200, status)

    def test_any_origin_header_is_refused(self) -> None:
        for origin in ("https://evil.example", "null", f"http://127.0.0.1:{self.live.server.port}"):
            status, _, _ = self.raw("POST", "/v1/rpc", body=self.rpc_bytes(), headers=self.auth(Origin=origin))
            self.assertEqual(403, status, origin)
            status, _, _ = self.raw("GET", "/v1/events?after=0", headers=self.auth(Origin=origin))
            self.assertEqual(403, status, origin)

    def test_content_type_length_and_size_are_enforced(self) -> None:
        status, _, _ = self.raw("POST", "/v1/rpc", body=self.rpc_bytes(), headers=self.auth(**{"Content-Type": "text/plain"}))
        self.assertEqual(415, status)
        status, _, _ = self.raw("POST", "/v1/rpc", body=self.rpc_bytes(), headers=self.auth(**{"Transfer-Encoding": "chunked"}))
        self.assertEqual(411, status)
        connection = http.client.HTTPConnection("127.0.0.1", self.live.server.port, timeout=10)
        connection.putrequest("POST", "/v1/rpc", skip_host=True)
        for name, value in {"Host": f"127.0.0.1:{self.live.server.port}", **self.auth(), "Content-Length": str(10 * 1024 * 1024)}.items():
            connection.putheader(name, value)
        connection.endheaders()
        self.assertEqual(413, connection.getresponse().status)
        connection.close()
        status, _, _ = self.raw("POST", "/v1/rpc", body=b" " * (65 * 1024), headers=self.auth())
        self.assertEqual(413, status)
        client = self.live.client(self.live.control)
        with self.assertRaises(ControlRpcError) as raised:
            client.call(
                "session.run",
                {"engine": "claude", "session": "s-one", "prompt": "p" * (65 * 1024)},
            )
        self.assertEqual(err.TOO_LARGE, raised.exception.code)
        self.assertEqual("body_too_large", raised.exception.message)

    def test_methods_and_paths_are_closed(self) -> None:
        for method, path, expected in (
            ("GET", "/v1/rpc", 404),
            ("POST", "/v1/events", 404),
            ("GET", "/", 404),
            ("GET", "/v1/../etc/passwd", 404),
            ("PUT", "/v1/rpc", 405),
            ("DELETE", "/v1/rpc", 405),
            ("OPTIONS", "/v1/rpc", 405),
            ("HEAD", "/v1/events", 405),
        ):
            status, _, _ = self.raw(method, path, headers=self.auth())
            self.assertEqual(expected, status, (method, path))

    def test_bad_query_strings_are_400(self) -> None:
        for query in ("after=abc", "after=1&after=2", "after=1&x=2", "after=-1", "limit=x", "wait=nan"):
            status, _, _ = self.raw("GET", f"/v1/events?{query}", headers=self.auth())
            self.assertIn(status, (400,), query)

    def test_responses_are_not_cacheable_and_do_not_name_the_runtime(self) -> None:
        status, headers, _ = self.raw("POST", "/v1/rpc", body=self.rpc_bytes(), headers=self.auth())
        self.assertEqual(200, status)
        self.assertEqual("no-store", headers["cache-control"])
        self.assertEqual("nosniff", headers["x-content-type-options"])
        self.assertEqual("control-api", headers["server"])
        self.assertNotIn("access-control-allow-origin", headers)

    def test_errors_never_echo_request_text(self) -> None:
        marker = "unique-marker-5521"
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": marker, "params": {marker: marker}}).encode()
        for payload in (body, marker.encode()):
            _, _, text = self.raw("POST", "/v1/rpc", body=payload, headers=self.auth())
            self.assertNotIn(marker, text.decode())


class BindTests(unittest.TestCase):
    def test_only_loopback_may_be_bound(self) -> None:
        stack = build_fake_stack()
        for host in ("0.0.0.0", "", "::", "::1", "192.0.2.1", "example.com", "127.0.0.2", "LOCALHOST"):
            with self.assertRaises(RemoteBindRefused, msg=host):
                ControlHTTPServer(stack.service, host)
        with ControlHTTPServer(stack.service, "localhost") as server:
            self.assertTrue(server.url.startswith("http://127.0.0.1:"))

    def test_the_client_refuses_a_non_loopback_url(self) -> None:
        for url in ("http://example.com:80", "https://127.0.0.1:1", "http://192.0.2.1:1", "http://127.0.0.1"):
            with self.assertRaises(ValueError, msg=url):
                ControlClient(url, "t" * 40)


class TokenFileTests(unittest.TestCase):
    def test_owner_only_create_exclusive_and_strict_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            identity = write_token_file(path, "t" * 40)
            info = path.lstat()
            self.assertEqual((info.st_dev, info.st_ino), identity)
            self.assertEqual("t" * 40, read_token_file(path))
            with self.assertRaises(FileExistsError):
                write_token_file(path, "other")
            with self.assertRaises(TokenFileRefused):
                read_token_file(Path(directory) / "missing")

    @unittest.skipIf(os.name == "nt", _SKIP_POSIX_TOKEN_MODE)
    def test_posix_owner_mode_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            write_token_file(path, "t" * 40)
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            os.chmod(path, 0o644)
            with self.assertRaises(TokenFileRefused) as raised:
                read_token_file(path)
            self.assertEqual("token_file_mode", str(raised.exception))

    @unittest.skipIf(os.name == "nt", _SKIP_TOKEN_SYMLINK)
    def test_a_symlink_token_path_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            write_token_file(path, "t" * 40)
            link = Path(directory) / "link"
            if os.name == "posix":
                # A POSIX failure to create the link fails the test. Skipping it
                # would drop the refusal check this platform is required to enforce.
                link.symlink_to(path)
            else:
                if not hasattr(os, "symlink"):
                    self.skipTest("os.symlink is unavailable")
                try:
                    link.symlink_to(path)
                except (OSError, NotImplementedError) as exc:
                    self.skipTest(f"symlinks unsupported: {exc}")
            with self.assertRaises(TokenFileRefused) as raised:
                read_token_file(link)
            self.assertEqual("token_file_not_regular", str(raised.exception))

    def test_a_fifo_token_path_is_refused(self) -> None:
        if not hasattr(os, "mkfifo"):
            self.skipTest("os.mkfifo is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "fifo"
            os.mkfifo(fifo)
            with self.assertRaises(TokenFileRefused):
                read_token_file(fifo)

    def test_bind_failure_removes_the_token_file_this_invocation_created(self) -> None:
        from scripts.v3.control_api.__main__ import main

        stack = build_fake_stack()
        blocker = ControlHTTPServer(stack.service).start()
        self.addCleanup(blocker.stop)
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "token"
            code = main(
                [
                    "serve-fake",
                    "--token-file",
                    str(token_file),
                    "--port",
                    str(blocker.port),
                ]
            )
            self.assertEqual(2, code)
            self.assertFalse(token_file.exists())

    def test_excess_connections_are_refused(self) -> None:
        stack = build_fake_stack()
        server = ControlHTTPServer(stack.service, max_connections=1).start()
        self.addCleanup(server.stop)
        token = stack.issue("op-a", "tenant-a", {SCOPE_CONTROL, SCOPE_OBSERVE})
        held = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        self.addCleanup(held.close)
        held.request(
            "GET",
            "/v1/events?after=0&wait=10",
            headers={"Authorization": f"Bearer {token}", "Host": f"127.0.0.1:{server.port}"},
        )
        time.sleep(0.2)
        client = ControlClient(server.url, token)
        with self.assertRaises(ControlRpcError) as raised:
            client.call("service.info")
        self.assertEqual(err.BUSY, raised.exception.code)
        self.assertEqual("too_many_connections", raised.exception.message)


class CliTests(unittest.TestCase):
    def test_serve_fake_call_and_events_without_printing_the_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "token"
            env = {**os.environ, "PYTHONPATH": str(ROOT)}
            server = subprocess.Popen(
                [sys.executable, "-X", "utf8", "-B", "-m", "scripts.v3.control_api", "serve-fake",
                 "--token-file", str(token_file)],
                cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                line = server.stdout.readline()
                info = json.loads(line)
                self.assertFalse(info["live_calls"])
                token = token_file.read_text().strip()
                self.assertNotIn(token, line)

                def cli(*args: str) -> subprocess.CompletedProcess:
                    return subprocess.run(
                        [sys.executable, "-X", "utf8", "-B", "-m", "scripts.v3.control_api", *args,
                         "--url", info["url"], "--token-file", str(token_file)],
                        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
                    )

                opened = cli("call", "session.open", json.dumps({"engine": "codex", "session": "s-one"}))
                ran = cli("call", "session.run", json.dumps({"engine": "codex", "session": "s-one", "prompt": PROMPT}))
                self.assertEqual(0, ran.returncode, ran.stderr)
                payload = json.loads(ran.stdout)
                self.assertEqual(200, payload["http_status"])
                self.assertEqual("ok", payload["response"]["result"]["result"]["status"])
                events = cli("events")
                self.assertEqual(0, events.returncode)
                self.assertEqual(
                    ["session.opened", "run.started", "run.finished"],
                    [e["kind"] for e in json.loads(events.stdout)["events"]],
                )
                for completed in (opened, ran, events):
                    self.assertNotIn(token, completed.stdout + completed.stderr)
                again = subprocess.run(
                    [sys.executable, "-X", "utf8", "-B", "-m", "scripts.v3.control_api", "serve-fake",
                     "--token-file", str(token_file)],
                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
                )
                self.assertEqual(2, again.returncode)
            finally:
                server.terminate()
                try:
                    server.wait(10)
                except subprocess.TimeoutExpired:
                    server.kill()
                server.stdout.close()
                server.stderr.close()

    @unittest.skipIf(os.name == "nt", _SKIP_POSIX_TOKEN_MODE)
    def test_cli_refuses_a_loose_token_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "token"
            env = {**os.environ, "PYTHONPATH": str(ROOT)}
            server = subprocess.Popen(
                [sys.executable, "-X", "utf8", "-B", "-m", "scripts.v3.control_api", "serve-fake",
                 "--token-file", str(token_file)],
                cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                info = json.loads(server.stdout.readline())
                token = token_file.read_text().strip()
                bad = Path(directory) / "loose"
                bad.write_text(token)
                os.chmod(bad, 0o644)
                refused = subprocess.run(
                    [sys.executable, "-X", "utf8", "-B", "-m", "scripts.v3.control_api", "call",
                     "--url", info["url"], "--token-file", str(bad), "service.info"],
                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
                )
                self.assertEqual(2, refused.returncode)
                self.assertIn("TokenFileRefused", refused.stderr)
                self.assertNotIn(token, refused.stdout + refused.stderr)
            finally:
                server.terminate()
                try:
                    server.wait(10)
                except subprocess.TimeoutExpired:
                    server.kill()
                server.stdout.close()
                server.stderr.close()

    @unittest.skipIf(os.name == "nt", _SKIP_SIGTERM_STOP)
    def test_sigterm_unlinks_the_token_file_and_the_path_can_be_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "token"
            env = {**os.environ, "PYTHONPATH": str(ROOT)}
            command = [
                sys.executable, "-X", "utf8", "-B", "-m", "scripts.v3.control_api", "serve-fake",
                "--token-file", str(token_file),
            ]

            def launch() -> subprocess.Popen[str]:
                return subprocess.Popen(
                    command,
                    cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )

            def finish(proc: subprocess.Popen[str]) -> None:
                if proc.poll() is None:
                    proc.send_signal(signal.SIGTERM)
                    try:
                        proc.wait(10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(10)
                if proc.stdout is not None:
                    proc.stdout.close()
                if proc.stderr is not None:
                    proc.stderr.close()

            server = launch()
            restarted: subprocess.Popen[str] | None = None
            try:
                info = json.loads(server.stdout.readline())
                self.assertFalse(info["live_calls"])
                self.assertTrue(token_file.is_file())
                self.assertIsNone(server.poll())
                server.send_signal(signal.SIGTERM)
                try:
                    code = server.wait(10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(10)
                    self.fail("serve-fake did not exit after SIGTERM")
                stderr = server.stderr.read() if server.stderr is not None else ""
                self.assertEqual(0, code, stderr)
                self.assertFalse(token_file.exists())

                restarted = launch()
                again_line = restarted.stdout.readline()
                if not again_line:
                    again_err = restarted.stderr.read() if restarted.stderr is not None else ""
                    self.fail(again_err or "serve-fake produced no ready line on restart")
                again = json.loads(again_line)
                self.assertFalse(again["live_calls"])
                self.assertTrue(token_file.is_file())
                self.assertNotIn("token_file_exists", again_line)
            finally:
                finish(server)
                if restarted is not None:
                    finish(restarted)

    @unittest.skipIf(os.name == "nt", _SKIP_SIGTERM_STOP)
    def test_sigterm_leaves_a_replaced_token_file_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "token"
            env = {**os.environ, "PYTHONPATH": str(ROOT)}
            command = [
                sys.executable, "-X", "utf8", "-B", "-m", "scripts.v3.control_api", "serve-fake",
                "--token-file", str(token_file),
            ]

            def launch() -> subprocess.Popen[str]:
                return subprocess.Popen(
                    command,
                    cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )

            def ready(proc: subprocess.Popen[str]) -> None:
                line = proc.stdout.readline() if proc.stdout is not None else ""
                if not line:
                    err_text = proc.stderr.read() if proc.stderr is not None else ""
                    self.fail(err_text or "serve-fake produced no ready line")
                info = json.loads(line)
                self.assertFalse(info["live_calls"])

            def finish(proc: subprocess.Popen[str]) -> None:
                if proc.poll() is None:
                    proc.send_signal(signal.SIGTERM)
                    try:
                        proc.wait(10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(10)
                if proc.stdout is not None:
                    proc.stdout.close()
                if proc.stderr is not None:
                    proc.stderr.close()

            first = launch()
            second: subprocess.Popen[str] | None = None
            try:
                ready(first)
                original = token_file.lstat()
                token_file.unlink()
                second = launch()
                ready(second)
                replaced = token_file.lstat()
                self.assertNotEqual(
                    (original.st_dev, original.st_ino), (replaced.st_dev, replaced.st_ino)
                )
                replacement = read_token_file(token_file)
                self.assertIsNone(first.poll())
                first.send_signal(signal.SIGTERM)
                try:
                    code = first.wait(10)
                except subprocess.TimeoutExpired:
                    first.kill()
                    first.wait(10)
                    self.fail("serve-fake did not exit after SIGTERM")
                stderr = first.stderr.read() if first.stderr is not None else ""
                self.assertEqual(0, code, stderr)
                self.assertEqual((replaced.st_dev, replaced.st_ino), _file_id(token_file))
                self.assertEqual(replacement, read_token_file(token_file))
                self.assertIsNone(second.poll())
                second.send_signal(signal.SIGTERM)
                try:
                    second_code = second.wait(10)
                except subprocess.TimeoutExpired:
                    second.kill()
                    second.wait(10)
                    self.fail("replacement serve-fake did not exit after SIGTERM")
                second_err = second.stderr.read() if second.stderr is not None else ""
                self.assertEqual(0, second_code, second_err)
                self.assertFalse(token_file.exists())
            finally:
                finish(first)
                if second is not None:
                    finish(second)

    def test_clean_stop_removes_an_open_token_file_and_restart_reuses_the_path(self) -> None:
        # Windows denies unlink while the exclusive-create descriptor is open
        # and serve-fake ignores that OSError. Simulate that denial on every
        # platform so a close-after-unlink regression fails here, not only in
        # Windows CI. POSIX coverage for unlink-while-open is the next test.
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "token"
            with self._token_unlink_guard(token_file, windows=True) as events:
                code, stdout, stderr = self._drive_clean_stop(token_file)
                self.assertEqual(0, code, stderr)
                self.assertIn('"live_calls": false', stdout)
                self.assertNotIn("token_file_exists", stderr)
                self.assertFalse(token_file.exists())
                self.assertEqual(["lstat", "close", "unlink"], events)
                events.clear()
                again, again_out, again_err = self._drive_clean_stop(token_file)
                self.assertEqual(0, again, again_err)
                self.assertIn('"live_calls": false', again_out)
                self.assertNotIn("token_file_exists", again_err)
                self.assertEqual(["lstat", "close", "unlink"], events)
                self.assertFalse(token_file.exists())

    @unittest.skipIf(os.name == "nt", _SKIP_POSIX_UNLINK_WHILE_OPEN)
    def test_posix_clean_stop_unlinks_while_the_token_descriptor_is_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "token"
            with self._token_unlink_guard(token_file, windows=False) as events:
                code, stdout, stderr = self._drive_clean_stop(token_file)
                self.assertEqual(0, code, stderr)
                self.assertIn('"live_calls": false', stdout)
                self.assertFalse(token_file.exists())
                self.assertEqual(["lstat", "unlink", "close"], events)
                events.clear()
                again, again_out, again_err = self._drive_clean_stop(token_file)
                self.assertEqual(0, again, again_err)
                self.assertIn('"live_calls": false', again_out)
                self.assertNotIn("token_file_exists", again_err)
                self.assertFalse(token_file.exists())

    def test_windows_startup_failure_removes_an_open_token_file(self) -> None:
        import scripts.v3.control_api.__main__ as cli

        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "token"

            def fail_start(server: object) -> None:
                raise OSError(98, "address already in use")

            with self._token_unlink_guard(token_file, windows=True) as events:
                with mock.patch.object(cli.ControlHTTPServer, "start", fail_start):
                    stderr = io.StringIO()
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                        code = cli.main(["serve-fake", "--token-file", str(token_file), "--port", "0"])
                self.assertIn("OSError", stderr.getvalue())
                self.assertEqual(2, code)
                self.assertFalse(token_file.exists())
                self.assertEqual(["lstat", "close", "unlink"], events)
                events.clear()
                again, again_out, again_err = self._drive_clean_stop(token_file)
                self.assertEqual(0, again, again_err)
                self.assertIn('"live_calls": false', again_out)
                self.assertNotIn("token_file_exists", again_err)
                self.assertFalse(token_file.exists())

    def _drive_clean_stop(self, token_file: Path) -> tuple[int, str, str]:
        import scripts.v3.control_api.__main__ as cli

        saved = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        stop = threading.Event()
        found = threading.Event()

        def watch() -> None:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if token_file.is_file():
                    found.set()
                    stop.set()
                    return
                time.sleep(0.01)
            stop.set()

        threading.Thread(target=watch, daemon=True).start()
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = cli._serve(
                    argparse.Namespace(token_file=str(token_file), port=0, tenant="tenant-demo"),
                    stop,
                )
        finally:
            for sig, handler in saved.items():
                signal.signal(sig, handler)
        self.assertTrue(found.is_set(), stderr.getvalue() or "serve-fake did not create the token file")
        return code, stdout.getvalue(), stderr.getvalue()

    @contextlib.contextmanager
    def _token_unlink_guard(self, token_file: Path, *, windows: bool):
        """Record identity-check, close, and unlink order for one token path.

        When ``windows`` is set, unlink raises if that path still names an open
        descriptor, which is what Windows does, and ``os.name`` is ``nt``.
        """

        import scripts.v3.control_api.__main__ as cli

        events: list[str] = []
        opened: list[int] = []
        real_lstat = cli.os.lstat
        real_unlink = cli.os.unlink
        real_close = cli.os.close
        real_create = cli.create_token_file
        target = os.fspath(token_file)

        def still_open(descriptor: int) -> bool:
            # Use the saved lstat. os.lstat is patched to watching_lstat below,
            # and calling it from here would recurse.
            try:
                held = os.fstat(descriptor)
                current = real_lstat(target)
            except OSError:
                return False
            return (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino)

        def tracking_create(path: str, token: str):
            device, inode, descriptor = real_create(path, token)
            opened.append(descriptor)
            return device, inode, descriptor

        def watching_lstat(path: str):
            if os.fspath(path) == target:
                events.append("lstat")
                self.assertTrue(opened and still_open(opened[-1]))
            return real_lstat(path)

        def guarded_unlink(path: str) -> None:
            if os.fspath(path) != target:
                real_unlink(path)
                return
            if windows and any(still_open(descriptor) for descriptor in opened):
                raise PermissionError(
                    13, "The process cannot access the file because it is being used by another process"
                )
            if not windows:
                self.assertTrue(any(still_open(descriptor) for descriptor in opened))
            events.append("unlink")
            real_unlink(path)

        def tracking_close(descriptor: int) -> None:
            if descriptor in opened:
                events.append("close")
            real_close(descriptor)

        patches = [
            mock.patch.object(cli.os, "lstat", watching_lstat),
            mock.patch.object(cli.os, "unlink", guarded_unlink),
            mock.patch.object(cli.os, "close", tracking_close),
            mock.patch.object(cli, "create_token_file", tracking_create),
        ]
        if windows:
            patches.append(mock.patch.object(cli.os, "name", "nt"))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            try:
                yield events
            finally:
                for descriptor in opened:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass


def _file_id(path: Path) -> tuple[int, int]:
    info = path.lstat()
    return info.st_dev, info.st_ino


if __name__ == "__main__":
    unittest.main()
