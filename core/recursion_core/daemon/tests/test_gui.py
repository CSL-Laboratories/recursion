"""Real loopback HTTP requests through the daemon's GUI composition root."""
from http.client import HTTPConnection
import importlib
import os
from pathlib import Path
import shutil
import sys
import tempfile
from unittest import TestCase, mock, skipIf
import socket
from wsgiref.simple_server import WSGIServer

from recursion_core.daemon import __main__ as entry
from recursion_core.daemon.gui import LoopbackGUI
from recursion_core.daemon.tests import plugin_fixture as fx
from recursion_core.gui import panels as gui_panels
from recursion_core.gui import surface as gui_surface
from recursion_core.gui.client import ControlClient
from recursion_core.gui.tests.fakes import FakeControlAPI, wsgi_transport

NOT_POSIX = os.name == 'nt' or not hasattr(os, 'getuid')
ENTRY_LINUX_ONLY = ('Linux-only: the daemon entry point needs flock, uid ownership checks and systemd; on Windows '
                    'main() refuses PlatformUnsupported (platform_unsupported) before any GUI is built; covered '
                    'by WindowsRefusalTests in daemon/tests/test_windows_refusal.py')
CROSS_REPO_PLUGIN_ENV = "RECURSION_CROSS_REPO_PLUGIN"
CROSS_REPO_SKIP = (f"cross-repo integration: {CROSS_REPO_PLUGIN_ENV} is unset; set it to the operator plugin's "
                   "pkg.module:registry factory, with that plugin on PYTHONPATH, to run this test")
CROSS_REPO_NEEDS = ("cross-repo integration needs Linux (uid ownership checks) and the cryptography package for "
                    "this interpreter: the daemon refuses to load any plugin it cannot verify")


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

    def test_declared_venue_filters_rows_after_the_read_through_the_daemons_gui_entry(self):
        # Through entry._start_gui (what the daemon's main() calls), not gui.compose: the page the daemon
        # serves filters a declared venue's rows after the read, in the one shared filter (gui/panels.py).
        self.assertIs(gui_surface.filter_venue_rows, gui_panels.filter_venue_rows)
        self.api.extra["operator.positions.inspect"] = {"result": {
            "positions": [{"venue": "kalshi", "symbol": "OWN-VENUE-ROW"}, {"venue": "othervenue", "symbol": "OTHER-VENUE-ROW"}],
            "total": "UNLABELLED-TOTAL"}}
        plugin = {"panels": [{"id": "test.positions", "title": "Positions panel", "venue": "kalshi",
                              "reads": ["operator.positions.inspect"]}]}
        with mock.patch.object(gui_surface, "filter_venue_rows", wraps=gui_panels.filter_venue_rows) as shared:
            gui = entry._start_gui(self.client, lambda _: True, (plugin,), 0, announced=False)
            try:
                status, _, page = self.request(gui, "/panel?id=test.positions")
            finally:
                gui.__exit__(None, None, None)
        self.assertEqual(status, 200)
        self.assertEqual([c[2]["command"] for c in self.api.calls], ["operator.positions.inspect"])  # an unscoped read
        self.assertEqual([call.args[1] for call in shared.call_args_list], ["kalshi"])
        symbols = [row["symbol"] for row in shared.call_args_list[0].args[0]["result"]["positions"]]
        self.assertEqual(symbols, ["OWN-VENUE-ROW", "OTHER-VENUE-ROW"])  # both rows came back from the read
        self.assertIn("OWN-VENUE-ROW", page)
        self.assertNotIn("OTHER-VENUE-ROW", page)
        self.assertNotIn("UNLABELLED-TOTAL", page)
        self.assertIn("Withheld: data not labelled <code>kalshi</code>", page)

    def test_cli_passes_plugin_directories_to_the_loader_and_imports_nothing(self):
        before = set(sys.modules)
        with mock.patch.object(entry, "require_free_threaded") as check, \
             mock.patch.object(entry, "main") as main, \
             mock.patch("sys.argv", ["daemon", "--plugin", "/opt/plugins/one", "--plugin", "/opt/plugins/two",
                                     "--gui-port", "0"]):
            entry.cli()
        main.assert_called_once_with(plugin_dirs=("/opt/plugins/one", "/opt/plugins/two"), port=0)
        self.assertGreaterEqual(check.call_count, 1)
        self.assertEqual(set(sys.modules) - before, set())
        self.assertFalse(hasattr(entry, "importlib"), "the entry point has no import-by-name path for plugins")
        # The old MODULE:REGISTRY form is refused as a usage error; it is never imported.
        with mock.patch.object(entry, "require_free_threaded"), mock.patch.object(entry, "main") as main, \
             mock.patch("sys.argv", ["daemon", "--plugin", "pkg.module:registry"]), \
             mock.patch("sys.stderr"), self.assertRaises(SystemExit) as stopped:
            entry.cli()
        self.assertEqual(stopped.exception.code, 2)
        main.assert_not_called()

    def test_loader_loads_the_real_cross_repo_plugin(self):
        # The real plugin's registry factory, reached only through the plugin loader: a one-line artifact
        # that imports the factory, with an installer grant for its panels. Nothing is mocked.
        documented = os.environ.get(CROSS_REPO_PLUGIN_ENV, "")
        if not documented:
            self.skipTest(CROSS_REPO_SKIP)
        from recursion_core.daemon import plugins as plugin_host
        from recursion_core.plugin_security.signing import backend_available
        if NOT_POSIX or not sys.platform.startswith("linux") or not backend_available():
            self.skipTest(CROSS_REPO_NEEDS)
        module, sep, name = documented.partition(":")
        self.assertTrue(sep and module and name.isidentifier(), f"{CROSS_REPO_PLUGIN_ENV} must be pkg.module:registry")
        importlib.import_module(module)  # set but not importable is a failure, not a skip
        tmp = Path(tempfile.mkdtemp(prefix="rc-cross-repo-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        os.chmod(tmp, 0o700)
        source = f"from {module} import {name} as registry\n".encode()
        config = tmp / "config" / "recursion"
        fx.write_install(config, grants=[fx.grant_for(source, plugin_id="cross.repo", permissions=("panel",))])
        fx.write_plugin(tmp / "cross", source, plugin_id="cross.repo", permissions=("panel",))
        from recursion_core.plugin_security.tests.support import FakeGilProbe
        # SIMULATED GIL state (off): this suite also runs on GIL builds, where the real probe poisons the core.
        host = plugin_host.PluginHost(config, tmp / "state", plugin_dirs=(tmp / "cross",), gil_probe=FakeGilProbe())
        self.addCleanup(host.close)
        record, = host.records
        self.assertEqual((record["state"], record["trust"], record["provides"]), ("loaded", "unsigned", ["panels"]), record)
        plugin, = host.registries
        self.assertTrue(plugin["panels"])
        with LoopbackGUI(self.client, lambda _: True, plugins=host.registries, port=0) as gui:
            self.assertEqual(self.request(gui)[0], 200)

    @skipIf(NOT_POSIX, ENTRY_LINUX_ONLY)
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

    @skipIf(NOT_POSIX, ENTRY_LINUX_ONLY)
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

    @skipIf(NOT_POSIX, ENTRY_LINUX_ONLY)
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

    @skipIf(NOT_POSIX, ENTRY_LINUX_ONLY)
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
