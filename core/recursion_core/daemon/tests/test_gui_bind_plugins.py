"""GUI bind retry must keep embedding plugin declarations that can be read once."""
from contextlib import ExitStack
import os
import socket
import sys
import unittest
from unittest.mock import Mock, patch

from recursion_core.daemon import __main__ as entry


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux daemon")
class GeneratorPluginBindRetryTests(unittest.TestCase):
    def test_F3_main_retains_generator_plugins_across_bind_retry(self):
        plugin = {"panels": (p for p in [{"id": "health", "title": "Health",
                                          "reads": ["operator.health.inspect"]}]),
                  "required": {"venue_use": (s for s in ["kalshi:model_fed_read"])}}
        occupied = socket.socket()
        self.addCleanup(occupied.close)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        stopped = Mock()
        calls = 0
        recovered = []
        start_gui = entry._start_gui

        def track_gui(*args, **kwargs):
            gui = start_gui(*args, **kwargs)
            if gui is not None:
                recovered.append(gui.plugins)
            return gui

        def wait(_):
            nonlocal calls
            calls += 1
            if calls == 1:
                occupied.close()
                return False
            return True

        stopped.wait.side_effect = wait
        with ExitStack() as stack:
            for name in ("require_free_threaded", "check_operator_linger", "UserLock",
                         "Registry", "Daemon", "SystemdUserManager"):
                stack.enter_context(patch.object(entry, name))
            stack.enter_context(patch.object(entry.signal, "signal"))
            stack.enter_context(patch.object(entry, "threading")).Event.return_value = stopped
            stack.enter_context(patch.object(entry, "_start_gui", side_effect=track_gui))
            stack.enter_context(patch.dict(os.environ, {"RECURSION_ENGINE_USER": ""}))
            entry.main(client=object(), door=bool, plugins=(plugin,), port=port)
        self.assertEqual(len(recovered), 1)
        snapshot = recovered[0][0]
        self.assertEqual([p["id"] for p in snapshot["panels"]], ["health"])
        self.assertEqual(snapshot["required"]["venue_use"], ("kalshi:model_fed_read",))


if __name__ == "__main__":
    unittest.main()
