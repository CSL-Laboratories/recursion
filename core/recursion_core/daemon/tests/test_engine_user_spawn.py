"""Cross-uid engine spawn, simulated. No sudo, no new OS user, no real setuid.

The id switch is an injected seam. Owner credential values below are fake markers.
Nothing opens a store path, a provider, logind or a user manager.
"""
import builtins
import os
import unittest
from unittest.mock import patch

from recursion_core.daemon import engine_user

OWNER_UID, OWNER_GID = 1000, 1000
ENGINE_UID, ENGINE_GID = 1001, 1001
OWNER_GROUPS = (4, 27, OWNER_GID)
ENGINE_GROUPS = (ENGINE_GID,)
SECRET = 'fake-owner-credential-value'
OWNER_HOME = '/home/operator'
ENGINE_HOME = '/var/lib/csl-engine'
OWNER_STORE = '/home/operator/.config/vendor/auth.json'
ENGINE_STORE = '/var/lib/csl-engine/.codex/auth.json'
ARGV = ['/opt/engine/bin/run', '--once']


def _identity(**overrides):
    identity = {
        'user': 'csl-engine',
        'uid': ENGINE_UID,
        'gid': ENGINE_GID,
        'groups': ENGINE_GROUPS,
        'home': ENGINE_HOME,
        'environ': {
            'PATH': '/usr/bin',
            'LANG': 'C',
            'ANTHROPIC_API_KEY': SECRET,
            'GITHUB_TOKEN': SECRET,
            'SSH_AUTH_SOCK': '/tmp/owner-ssh-agent',
            'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/1000/bus',
            'RECURSION_ENGINE_USER': 'csl-engine',
            'HOME': OWNER_HOME,
            'USER': 'operator',
        },
    }
    identity.update(overrides)
    return identity


def _owner_env():
    return {
        'HOME': OWNER_HOME,
        'USER': 'operator',
        'PATH': '/home/operator/bin:/usr/bin',
        'ANTHROPIC_API_KEY': SECRET,
        'GITHUB_TOKEN': SECRET,
        'GH_TOKEN': SECRET,
        'AWS_SECRET_ACCESS_KEY': SECRET,
        'SSH_AUTH_SOCK': '/tmp/owner-ssh-agent',
        'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/1000/bus',
        'RECURSION_ENGINE_USER': 'csl-engine',
        'LANG': SECRET,
    }


def _store(path, uid, gid, mode):
    return {'path': path, 'owner_uid': uid, 'owner_gid': gid, 'mode': mode}


class SimulatedSwitch:
    """In-memory setgroups/setgid/setuid. A drop to a non-owner uid blocks later id calls."""

    def __init__(self, *, fail=None, stick=True):
        self.uid = OWNER_UID
        self.gid = OWNER_GID
        self.groups = list(OWNER_GROUPS)
        self.calls = []
        self.fail = fail
        self.stick = stick
        self._dropped = False

    def _reject_if_dropped(self, name):
        if self._dropped:
            raise PermissionError(f'{name} after setuid')

    def setgroups(self, groups):
        self.calls.append(('setgroups', tuple(groups)))
        if self.fail == 'setgroups':
            raise PermissionError('setgroups refused')
        self._reject_if_dropped('setgroups')
        self.groups = list(groups)

    def setgid(self, gid):
        self.calls.append(('setgid', gid))
        if self.fail == 'setgid':
            raise PermissionError('setgid refused')
        self._reject_if_dropped('setgid')
        self.gid = gid

    def setuid(self, uid):
        self.calls.append(('setuid', uid))
        if self.fail == 'setuid':
            raise PermissionError('setuid refused')
        if self.stick:
            self.uid = uid
            self._dropped = True

    def getuid(self):
        return self.uid

    def getgid(self):
        return self.gid

    def getgroups(self):
        return tuple(self.groups)


class SpawnTests(unittest.TestCase):
    def spawn(self, *args, **kwargs):
        return engine_user.spawn_as_engine_user(*args, **kwargs)

    def test_refusal_default_when_no_engine_user_is_configured(self):
        started = []
        switch = SimulatedSwitch()

        def start(*args):
            started.append(args)

        unconfigured = engine_user.EngineUserUnconfigured
        with self.assertRaises(unconfigured) as bare:
            self.spawn(ARGV, start=start)
        with self.assertRaises(unconfigured):
            self.spawn(ARGV, identity=None, start=start, owner_env=_owner_env())
        # An identity object does not arm a real switch. The default stays a refusal.
        with self.assertRaises(unconfigured):
            self.spawn(ARGV, identity=_identity(), start=start, owner_env=_owner_env())
        with self.assertRaises(ValueError):
            self.spawn(ARGV, identity={}, id_switch=switch, start=start)
        with self.assertRaises(ValueError):
            self.spawn(ARGV, identity='csl-engine', id_switch=switch, start=start)
        self.assertEqual(started, [])
        self.assertEqual(switch.calls, [])
        self.assertNotIn(SECRET, str(bare.exception))
        self.assertNotIn('csl-engine', str(bare.exception))

    def test_spawn_drops_uid_gid_groups_and_environment(self):
        switch = SimulatedSwitch()
        started = []

        def start(argv, env, creds):
            started.append((list(argv), dict(env), creds))

        result = self.spawn(
            ARGV, identity=_identity(), id_switch=switch, start=start,
            owner_uid=OWNER_UID, owner_env=_owner_env(),
            owner_stores=[_store(OWNER_STORE, OWNER_UID, OWNER_GID, 0o600)],
            own_stores=[_store(ENGINE_STORE, ENGINE_UID, ENGINE_GID, 0o600)])

        self.assertEqual([name for name, _ in switch.calls], ['setgroups', 'setgid', 'setuid'])
        self.assertEqual(switch.calls[0][1], ENGINE_GROUPS)
        self.assertEqual(switch.calls[1][1], ENGINE_GID)
        self.assertEqual(switch.calls[2][1], ENGINE_UID)
        self.assertEqual(result['uid'], ENGINE_UID)
        self.assertEqual(result['gid'], ENGINE_GID)
        self.assertEqual(result['groups'], ENGINE_GROUPS)
        self.assertNotEqual(set(result['groups']), set(OWNER_GROUPS))
        env = result['env']
        self.assertEqual(env['HOME'], ENGINE_HOME)
        self.assertEqual(env['USER'], 'csl-engine')
        self.assertEqual(env['LOGNAME'], 'csl-engine')
        self.assertEqual(env['PATH'], '/usr/bin')
        self.assertEqual(env['LANG'], 'C')
        for name in ('ANTHROPIC_API_KEY', 'GITHUB_TOKEN', 'GH_TOKEN', 'AWS_SECRET_ACCESS_KEY',
                     'SSH_AUTH_SOCK', 'DBUS_SESSION_BUS_ADDRESS', 'RECURSION_ENGINE_USER'):
            self.assertNotIn(name, env)
        for value in env.values():
            self.assertNotIn(SECRET, value)
            self.assertNotIn(OWNER_HOME, value)
            self.assertNotIn(OWNER_STORE, value)
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0][0], ARGV)
        self.assertEqual(started[0][1], env)
        self.assertEqual(started[0][2], (ENGINE_UID, ENGINE_GID, ENGINE_GROUPS))

    def test_failed_switch_does_not_start_the_command(self):
        failed = engine_user.EngineSwitchFailed
        for fail in ('setgroups', 'setgid', 'setuid'):
            switch = SimulatedSwitch(fail=fail)
            started = []
            with self.subTest(fail=fail), self.assertRaises(failed):
                self.spawn(ARGV, identity=_identity(), id_switch=switch,
                           start=lambda *args: started.append(args),
                           owner_uid=OWNER_UID, owner_env=_owner_env())
            self.assertEqual(started, [])
            self.assertNotIn('setuid', [name for name, _ in switch.calls] if fail != 'setuid' else [])
            if fail == 'setuid':
                self.assertEqual([name for name, _ in switch.calls], ['setgroups', 'setgid', 'setuid'])
            if fail == 'setgid':
                self.assertEqual([name for name, _ in switch.calls], ['setgroups', 'setgid'])
            if fail == 'setgroups':
                self.assertEqual([name for name, _ in switch.calls], ['setgroups'])

        switch = SimulatedSwitch(stick=False)
        started = []
        with self.assertRaises(failed):
            self.spawn(ARGV, identity=_identity(), id_switch=switch,
                       start=lambda *args: started.append(args),
                       owner_uid=OWNER_UID)
        self.assertEqual(started, [])
        self.assertEqual(switch.getuid(), OWNER_UID)

        started = []
        with self.assertRaises(engine_user.EngineSpawnRefused):
            self.spawn(ARGV, identity=_identity(uid=OWNER_UID), id_switch=SimulatedSwitch(),
                       start=lambda *args: started.append(args), owner_uid=OWNER_UID)
        self.assertEqual(started, [])

    def test_engine_cannot_read_owner_stores(self):
        owner = _store(OWNER_STORE, OWNER_UID, OWNER_GID, 0o600)
        own = _store(ENGINE_STORE, ENGINE_UID, ENGINE_GID, 0o600)
        switch = SimulatedSwitch()
        started = []

        def refuse_open(*args, **kwargs):
            raise AssertionError('store path opened')

        with patch.object(builtins, 'open', refuse_open), patch.object(os, 'open', refuse_open):
            result = self.spawn(
                ARGV, identity=_identity(), id_switch=switch,
                start=lambda *args: started.append(args),
                owner_uid=OWNER_UID, owner_env=_owner_env(),
                owner_stores=[owner], own_stores=[own])
        self.assertEqual(len(started), 1)
        self.assertFalse(result['stores'][OWNER_STORE])
        self.assertTrue(result['own_stores'][ENGINE_STORE])
        self.assertNotIn(OWNER_STORE, result['env'].values())

        started = []
        world = _store(OWNER_STORE, OWNER_UID, OWNER_GID, 0o644)
        with self.assertRaises(engine_user.EngineSpawnRefused):
            self.spawn(ARGV, identity=_identity(), id_switch=SimulatedSwitch(),
                       start=lambda *args: started.append(args),
                       owner_uid=OWNER_UID, owner_stores=[world])
        self.assertEqual(started, [])

        started = []
        grouped = _store(OWNER_STORE, OWNER_UID, 4, 0o640)
        leaked = _identity(groups=(ENGINE_GID, 4))
        with self.assertRaises(engine_user.EngineSpawnRefused):
            self.spawn(ARGV, identity=leaked, id_switch=SimulatedSwitch(),
                       start=lambda *args: started.append(args),
                       owner_uid=OWNER_UID, owner_stores=[grouped])
        self.assertEqual(started, [])

    def test_session_launcher_does_not_switch(self):
        switch = SimulatedSwitch()
        started = []
        with self.assertRaises(engine_user.SessionLaunchRefused):
            self.spawn(['su', '-', 'csl-engine'], identity=_identity(), id_switch=switch,
                       start=lambda *args: started.append(args), owner_uid=OWNER_UID)
        self.assertEqual(started, [])
        self.assertEqual(switch.calls, [])

    def test_engine_home_inside_owner_home_refuses(self):
        switch = SimulatedSwitch()
        started = []
        with self.assertRaises(engine_user.EngineSpawnRefused):
            self.spawn(ARGV, identity=_identity(home=OWNER_HOME + '/engine'),
                       id_switch=switch, start=lambda *args: started.append(args),
                       owner_uid=OWNER_UID, owner_env=_owner_env())
        self.assertEqual(started, [])
        self.assertEqual(switch.calls, [])

    def test_second_caller_is_independent(self):
        first = SimulatedSwitch()
        second = SimulatedSwitch()
        other = _identity(user='csl-other', uid=1002, gid=1002, groups=(1002,),
                          home='/var/lib/csl-other')
        # The second simulator still starts at the owner and must end on 1002, not 1001.
        one = self.spawn(ARGV, identity=_identity(), id_switch=first, start=lambda *a: None,
                         owner_uid=OWNER_UID, owner_env=_owner_env())
        two = self.spawn(ARGV, identity=other, id_switch=second, start=lambda *a: None,
                         owner_uid=OWNER_UID, owner_env=_owner_env())
        self.assertEqual(one['uid'], ENGINE_UID)
        self.assertEqual(two['uid'], 1002)
        self.assertEqual(two['env']['HOME'], '/var/lib/csl-other')
        self.assertEqual(two['env']['USER'], 'csl-other')
        self.assertEqual(first.getuid(), ENGINE_UID)
        self.assertEqual(second.getuid(), 1002)

    def test_start_exception_propagates_and_a_later_call_still_runs(self):
        def boom(*args):
            raise RuntimeError('start failed')

        with self.assertRaisesRegex(RuntimeError, 'start failed'):
            self.spawn(ARGV, identity=_identity(), id_switch=SimulatedSwitch(), start=boom,
                       owner_uid=OWNER_UID)
        switch = SimulatedSwitch()
        result = self.spawn(ARGV, identity=_identity(), id_switch=switch, start=lambda *a: None,
                            owner_uid=OWNER_UID)
        self.assertEqual(result['uid'], ENGINE_UID)
        self.assertEqual(switch.getuid(), ENGINE_UID)

    def test_interrupt_during_switch_does_not_start(self):
        class Interrupted(SimulatedSwitch):
            def setuid(self, uid):
                self.calls.append(('setuid', uid))
                raise KeyboardInterrupt

        started = []
        with self.assertRaises(KeyboardInterrupt):
            self.spawn(ARGV, identity=_identity(), id_switch=Interrupted(),
                       start=lambda *args: started.append(args), owner_uid=OWNER_UID)
        self.assertEqual(started, [])


if __name__ == '__main__':
    unittest.main()
