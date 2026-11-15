# Model: Claude Opus 5.5 (claude-opus-5-5), lane SESSIONS.
"""The registrant registers nothing live; ``install`` registers exactly this slice's rows and the service."""
from __future__ import annotations

import unittest

from recursion_core.control import hooks as H
from recursion_core.control.methods import METHODS
from recursion_core.sessions import hooks as session_hooks
from recursion_core.sessions.tests.support import Lane


class RegistrantTest(unittest.TestCase):

    def test_the_registrant_is_the_name_reserved_on_the_fixed_list(self):
        self.assertIn('recursion_core.sessions.hooks', H.REGISTRANTS)
        self.assertEqual(session_hooks.__name__, 'recursion_core.sessions.hooks')

    def test_register_registers_nothing(self):
        hooks = H.Hooks()
        self.assertIsNone(session_hooks.register(hooks))
        hooks.freeze()
        self.assertEqual(hooks.methods.registered(), ())
        self.assertEqual(hooks.commands.names(), ())
        self.assertEqual(hooks._services, [])
        self.assertFalse(hooks.start_on)
        self.assertFalse(hooks.gate.enabled)

    def test_a_composition_with_this_module_installed_has_nothing_of_this_lane(self):
        composed = H.compose()
        self.assertEqual(composed.methods.registered(), ())
        self.assertEqual(composed.commands.names(), ())
        self.assertFalse(composed.gate.enabled)

    def test_install_registers_this_slice_s_rows_with_their_published_service_and_the_lane_s_service(self):
        lane = Lane()
        mine = {name for name in METHODS if name.startswith(('session.', 'queue.'))}
        registered = set(lane.hooks.methods.registered())
        self.assertEqual(registered, {'session.start', 'session.turn', 'session.steer', 'session.queue',
                                      'session.status', 'session.list', 'queue.update', 'queue.cancel'})
        self.assertEqual(mine - registered, {'session.interrupt', 'session.resume', 'session.fork', 'session.export',
                                             'session.stop'})
        self.assertEqual(lane.hooks.methods.services_needed(), {})
        self.assertEqual([service.name for service in lane.hooks._services], ['sessions'])


if __name__ == '__main__':
    unittest.main()
