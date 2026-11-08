"""Real loopback HTTP requests through the daemon's GUI composition root."""
from http.client import HTTPConnection
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

    def test_bad_plugin_does_not_leave_a_bound_socket(self):
        servers = []
        def bind(*args):
            server = WSGIServer(*args)
            servers.append(server)
            return server
        with mock.patch("recursion_core.daemon.gui.WSGIServer", side_effect=bind):
            gui = LoopbackGUI(self.client, lambda _: True,
                              plugins=({"panels": [{"id": "bad", "title": "Bad", "reads": ["stop"]}]},), port=0)
            with self.assertRaises(ValueError):
                gui.__enter__()
        self.assertIsNone(gui.server)
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0].socket.fileno(), -1)

    def test_cli_registers_explicit_plugin_and_checks_runtime_after_import(self):
        plugin = {"panels": []}
        with mock.patch.object(entry, "require_free_threaded") as check, \
             mock.patch.object(entry.importlib, "import_module") as load, \
             mock.patch.object(entry, "main") as main, \
             mock.patch("sys.argv", ["daemon", "--plugin", "cockpit.gui:registry", "--gui-port", "0"]):
            load.return_value = SimpleNamespace(registry=lambda: plugin)
            entry.cli()
            load.assert_called_once_with("cockpit.gui")
            main.assert_called_once_with(plugins=[plugin], port=0)
            self.assertEqual(check.call_count, 3)

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
