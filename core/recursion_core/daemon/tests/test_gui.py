"""Real loopback HTTP requests through the daemon's GUI composition root."""
from http.client import HTTPConnection
import importlib
from pathlib import Path
from unittest import TestCase, mock
import socket
from types import SimpleNamespace
from wsgiref.simple_server import WSGIServer

from recursion_core.daemon import __main__ as entry
from recursion_core.daemon.gui import LoopbackGUI
from recursion_core.gui.client import ControlClient
from recursion_core.gui.tests.fakes import FakeControlAPI, wsgi_transport


class LoopbackTests(TestCase):
    def setUp(self):
        self.api = FakeControlAPI()
        self.client = ControlClient(wsgi_transport(self.api))

    def request(self, gui, path="/", *, method="GET", headers=None):
        connection = HTTPConnection(*gui.address, timeout=3)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally:
            connection.close()

    def test_real_get_serves_the_page_and_closes_the_socket(self):
        authenticated = []
        def door(env):
            authenticated.append(env["PATH_INFO"])
            return True  # injected test device adapter, never a production default
        with LoopbackGUI(self.client, door, port=0) as gui:
            address = gui.address
            self.assertEqual(address[0], "127.0.0.1")
            status, headers, page = self.request(gui)
            self.assertEqual(status, 200)
            self.assertIn("text/html", headers["Content-Type"])
            self.assertIn("<h1>Recursion operator</h1>", page)
            self.assertIn("gap:engine:gemini", page)
            self.assertEqual(authenticated, ["/"])
        self.assertFalse(gui.thread.is_alive())
        with socket.socket() as connection:
            self.assertNotEqual(connection.connect_ex(address), 0)

    def test_missing_adapter_fails_closed_before_the_control_api(self):
        with LoopbackGUI(self.client, port=0) as gui:
            status, _, body = self.request(gui)
            self.assertEqual(status, 401)
            self.assertIn("device_not_authenticated", body)
            self.assertEqual(self.api.calls, [])

    def test_host_and_origin_guards_are_preserved_on_the_wire(self):
        with LoopbackGUI(self.client, lambda _: True, port=0) as gui:
            for headers in ({"Host": "attacker.invalid"}, {"Origin": "http://attacker.invalid"},
                            {"Sec-Fetch-Site": "cross-site"}):
                with self.subTest(headers=headers):
                    self.assertEqual(self.request(gui, headers=headers)[0], 403)
        self.assertEqual(self.api.calls, [])

    def test_read_only_plugin_panel_is_served_on_the_same_door(self):
        plugin = {"panels": [{"id": "test.health", "title": "Health panel",
                              "reads": ["operator.health.inspect"]}]}
        with LoopbackGUI(self.client, lambda _: True, plugins=(plugin,), port=0) as gui:
            status, _, page = self.request(gui, "/panel?id=test.health")
            self.assertEqual(status, 200)
            self.assertIn("Health panel", page)
            main = page.split('<main id="main"', 1)[1].split('</main>', 1)[0]
            self.assertNotIn("<form", main)
            self.assertEqual([c[2]["command"] for c in self.api.calls], ["operator.health.inspect"])

    def test_generator_backed_plugin_panels_survive_startup_validation(self):
        # Devin r4507809767: the constructor-time check must not exhaust one-shot iterables.
        plugin = {"panels": (p for p in [{"id": "health", "title": "Health",
                                          "reads": ["operator.health.inspect"]}])}
        with LoopbackGUI(self.client, lambda _: True, plugins=(plugin,), port=0) as gui:
            status, _, page = self.request(gui, "/panel?id=health")
            self.assertEqual(status, 200)
            self.assertIn("Health", page)
            self.assertEqual([c[2]["command"] for c in self.api.calls], ["operator.health.inspect"])

    def test_generator_backed_required_routes_survive_startup_validation(self):
        plugin = {"required": {"venue_use": (s for s in ["kalshi:model_fed_read"])}}
        with LoopbackGUI(self.client, lambda _: True, plugins=(plugin,), port=0) as gui:
            status, _, page = self.request(gui)
            self.assertEqual(status, 200)
            self.assertIn("gap:venue_use:kalshi:model_fed_read", page)

    def test_plugin_that_is_not_a_mapping_is_refused_before_any_bind(self):
        with mock.patch("recursion_core.daemon.gui.WSGIServer") as server:
            with self.assertRaises(ValueError):
                LoopbackGUI(self.client, lambda _: True, plugins=([("panels", ())],), port=0)
        server.assert_not_called()

    def test_bad_plugin_does_not_leave_a_bound_socket(self):
        servers = []
        def bind(*args):
            server = WSGIServer(*args)
            servers.append(server)
            return server
        with mock.patch("recursion_core.daemon.gui.WSGIServer", side_effect=bind):
            with self.assertRaises(ValueError):
                LoopbackGUI(self.client, lambda _: True,
                            plugins=({"panels": [{"id": "bad", "title": "Bad", "reads": ["stop"]}]},), port=0)
        self.assertEqual(servers, [])  # rejected before any socket is bound

    def test_cli_registers_explicit_plugin_and_checks_runtime_after_import(self):
        plugin = {"panels": []}
        with mock.patch.object(entry, "require_free_threaded") as check, \
             mock.patch.object(entry, "importlib") as imports, \
             mock.patch.object(entry, "main") as main, \
             mock.patch("sys.argv", ["daemon", "--plugin", "cockpit.gui.panels:registry", "--gui-port", "0"]):
            imports.import_module.return_value = SimpleNamespace(registry=lambda: plugin)
            entry.cli()
            imports.import_module.assert_called_once_with("cockpit.gui.panels")
            main.assert_called_once_with(plugins=[plugin], port=0)
            self.assertEqual(check.call_count, 3)

    def test_cli_documented_cockpit_plugin_imports_for_real(self):
        # Unmocked import of the path documented in --help and the README.
        try:
            importlib.import_module("cockpit.gui.panels")
        except ModuleNotFoundError as exc:
            if exc.name not in {"cockpit", "cockpit.gui", "cockpit.gui.panels"}:
                raise
            self.skipTest("run with Cockpit #318 engine on PYTHONPATH for cross-repo integration")
        documented = "cockpit.gui.panels:registry"
        self.assertIn(documented, Path(entry.__file__).read_text())  # the --help example
        with mock.patch.object(entry, "require_free_threaded"), \
             mock.patch.object(entry, "main") as main, \
             mock.patch("sys.argv", ["daemon", "--plugin", documented, "--gui-port", "0"]):
            entry.cli()
        plugin, = main.call_args.kwargs["plugins"]
        self.assertIn("panels", plugin)
        self.assertTrue(plugin["panels"])

    def test_service_entry_point_starts_and_closes_gui(self):
        plugin = {"panels": []}
        with mock.patch.object(entry, "require_free_threaded"), \
             mock.patch.object(entry, "check_operator_linger"), \
             mock.patch.object(entry.signal, "signal"), \
             mock.patch.object(entry, "UserLock"), \
             mock.patch.object(entry, "Registry"), \
             mock.patch.object(entry, "SystemdUserManager"), \
             mock.patch.object(entry, "Daemon") as daemon, \
             mock.patch.object(entry, "LoopbackGUI") as gui, \
             mock.patch.object(entry.threading, "Event") as stopped, \
             mock.patch.dict(entry.os.environ, {"RECURSION_ENGINE_USER": ""}):
            stopped.return_value.wait.return_value = True
            entry.main(client=self.client, door=bool, plugins=(plugin,), port=0)
            gui.assert_called_once_with(self.client, bool, plugins=(plugin,), port=0)
            gui.return_value.__enter__.assert_called_once()
            gui.return_value.__exit__.assert_called_once()
            daemon.return_value.close.assert_called_once()

    def test_occupied_gui_port_keeps_reconciliation_running_and_retries(self):
        occupant = socket.socket()
        occupant.bind(("127.0.0.1", 0))
        occupant.listen()
        port = occupant.getsockname()[1]
        seen = []
        def wait(_timeout):
            tick = len(seen)
            if tick == 1:
                occupant.close()  # the unrelated listener goes away
            if tick == 2:  # one sweep after the retry: our GUI now owns the port
                connection = HTTPConnection("127.0.0.1", port, timeout=3)
                try:
                    connection.request("GET", "/")
                    response = connection.getresponse()
                    seen.append((response.status, response.read().decode()))
                finally:
                    connection.close()
                return True
            seen.append(None)
            return False
        try:
            with mock.patch.object(entry, "require_free_threaded"), \
                 mock.patch.object(entry, "check_operator_linger"), \
                 mock.patch.object(entry.signal, "signal"), \
                 mock.patch.object(entry, "UserLock"), \
                 mock.patch.object(entry, "Registry"), \
                 mock.patch.object(entry, "SystemdUserManager"), \
                 mock.patch.object(entry, "Daemon") as daemon, \
                 mock.patch.object(entry, "threading") as threads, \
                 mock.patch.dict(entry.os.environ, {"RECURSION_ENGINE_USER": ""}), \
                 self.assertLogs("recursion_core.daemon", "INFO") as logs:
                threads.Event.return_value.wait.side_effect = wait  # module-local: real GUI threads keep real Events
                entry.main(client=self.client, port=port)
        finally:
            occupant.close()
        # Initial sweep plus one per elapsed tick: the loop survived the failed bind.
        self.assertEqual(daemon.return_value.sweep.call_count, 3)
        daemon.return_value.close.assert_called_once()
        status, body = seen[-1]
        self.assertEqual(status, 401)  # default door: fails closed, but the page is ours
        self.assertIn("device_not_authenticated", body)
        warnings = [line for line in logs.output if "GUI unavailable" in line]
        self.assertEqual(len(warnings), 1)  # logged once, not every sweep
        self.assertTrue(any("GUI available again" in line for line in logs.output))
        with socket.socket() as probe:
            self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0)

    def test_bad_plugin_registry_still_stops_the_service(self):
        with mock.patch.object(entry, "require_free_threaded"), \
             mock.patch.object(entry, "check_operator_linger"), \
             mock.patch.object(entry.signal, "signal"), \
             mock.patch.object(entry, "UserLock"), \
             mock.patch.object(entry, "Registry"), \
             mock.patch.object(entry, "SystemdUserManager"), \
             mock.patch.object(entry, "Daemon") as daemon, \
             mock.patch.dict(entry.os.environ, {"RECURSION_ENGINE_USER": ""}):
            bad = {"panels": [{"id": "bad", "title": "Bad", "reads": ["stop"]}]}
            with self.assertRaises(ValueError):
                entry.main(client=self.client, plugins=(bad,), port=0)
            daemon.return_value.close.assert_called_once()

    def test_bad_plugin_registry_stops_the_service_even_when_the_port_is_occupied(self):
        occupant = socket.socket()
        occupant.bind(("127.0.0.1", 0))
        occupant.listen()
        port = occupant.getsockname()[1]
        bad = {"panels": [{"id": "bad", "title": "Bad", "reads": ["stop"]}]}
        try:
            with mock.patch.object(entry, "require_free_threaded"), \
                 mock.patch.object(entry, "check_operator_linger"), \
                 mock.patch.object(entry.signal, "signal"), \
                 mock.patch.object(entry, "UserLock"), \
                 mock.patch.object(entry, "Registry"), \
                 mock.patch.object(entry, "SystemdUserManager"), \
                 mock.patch.object(entry, "Daemon") as daemon, \
                 mock.patch.object(entry, "threading") as threads, \
                 mock.patch.dict(entry.os.environ, {"RECURSION_ENGINE_USER": ""}):
                threads.Event.return_value.wait.side_effect = [False, False, True]  # bounded if tolerated
                with self.assertRaises(ValueError):
                    entry.main(client=self.client, plugins=(bad,), port=port)
        finally:
            occupant.close()
        # Fails at startup, before the reconciliation loop, not on a later retry.
        self.assertEqual(daemon.return_value.sweep.call_count, 1)
        threads.Event.return_value.wait.assert_not_called()
        daemon.return_value.close.assert_called_once()
