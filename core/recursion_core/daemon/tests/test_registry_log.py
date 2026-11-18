"""A failed reconcile after a failed launch must not log the launch argv.

E16: Daemon.start catches a launch failure. When the follow-up reconcile also
fails, the record names that error's type and nothing else. The reconcile runs
after the launch handler, so a record written while it runs does not chain the
launch argv.
"""
import io
import json
import logging
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import recursion_core.daemon as core

LINUX = sys.platform.startswith('linux')
MARKER = 'SYNTHETIC-NOT-A-CREDENTIAL'


class Grab(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


class Boom(BaseException):
    """A non-Exception failure from the reconcile. Not an interrupt."""


class Scripted:
    def __init__(self, store, *, observe='not-found', processes=True, on_start=None, live=None):
        self.store = store
        self.observe_value = observe
        self.processes = processes
        self.on_start = on_start
        self.live = live

    def start(self, unit, argv):
        if self.on_start is not None:
            self.on_start()
        raise subprocess.CalledProcessError(1, argv)

    def stop(self, unit):
        return None

    def observe(self, unit):
        if isinstance(self.observe_value, BaseException):
            raise self.observe_value
        return self.observe_value

    def processes_gone(self, unit):
        if isinstance(self.processes, BaseException):
            raise self.processes
        return self.processes is True

    def owned_units(self):
        return set()

    def recorded_cgroup_live(self, unit, group):
        if isinstance(self.live, BaseException):
            raise self.live
        return self.live


@unittest.skipUnless(LINUX, 'the daemon is Linux only')
class RegistryLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='registry-log-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.grab = Grab()
        core.log.addHandler(self.grab)
        self.addCleanup(core.log.removeHandler, self.grab)

    def store(self, directory=None):
        opened = core.Registry(directory or self.root / 'state')
        self.addCleanup(opened.close)
        return opened

    def text_of(self, records):
        formatter = logging.Formatter()
        parts = []
        for record in records:
            parts.append(formatter.format(record))
        return '\n'.join(parts)

    def errors(self):
        return [record for record in self.grab.records if record.levelno >= logging.ERROR]

    def assert_launch_error(self, daemon, argv):
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            daemon.start('one', argv)
        self.assertIn(MARKER, str(caught.exception))
        return caught.exception

    def test_F8_reconcile_failure_log_does_not_include_launch_argv(self):
        store = self.store()
        manager = core.SystemdUserManager()

        def call(argv):
            if argv[0] == 'systemd-run':
                raise subprocess.CalledProcessError(1, argv)
            return ('LoadState=not-found\nActiveState=inactive\nMainPID=0\n'
                    'ControlGroup=\n')

        manager._call = call
        daemon = core.Daemon(store, manager, lambda: 'boot-a')
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        core.log.addHandler(handler)
        try:
            with patch.object(store, 'state',
                              side_effect=sqlite3.OperationalError('test disk full')):
                with self.assertRaises(subprocess.CalledProcessError):
                    daemon.start('one', ['not-executed', '--api-key', MARKER])
        finally:
            core.log.removeHandler(handler)
            handler.close()
        self.assertNotIn(MARKER, stream.getvalue())
        errors = self.errors()
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].levelno, logging.ERROR)
        self.assertEqual(
            errors[0].getMessage(),
            'reconcile after launch failure of one failed: OperationalError')
        rendered = self.text_of(errors)
        self.assertNotIn(MARKER, rendered)
        self.assertNotIn('test disk full', rendered)
        self.assertNotIn('Traceback', rendered)

    def test_a_non_sqlite_reconcile_failure_logs_the_type_name_only(self):
        store = self.store()
        manager = core.SystemdUserManager()

        def call(argv):
            if argv[0] == 'systemd-run':
                raise subprocess.CalledProcessError(1, argv)
            return ('LoadState=not-found\nActiveState=inactive\nMainPID=0\n'
                    'ControlGroup=\n')

        manager._call = call
        daemon = core.Daemon(store, manager, lambda: 'boot-a')
        with patch.object(store, 'state', side_effect=RuntimeError('boom-not-sqlite')):
            with self.assertRaises(subprocess.CalledProcessError):
                daemon.start('one', ['not-executed', '--api-key', MARKER])
        errors = self.errors()
        self.assertEqual(len(errors), 1)
        self.assertEqual(
            errors[0].getMessage(),
            'reconcile after launch failure of one failed: RuntimeError')
        rendered = self.text_of(errors)
        self.assertNotIn(MARKER, rendered)
        self.assertNotIn('boom-not-sqlite', rendered)
        self.assertNotIn('Traceback', rendered)

    def test_a_nested_attestation_failure_does_not_log_the_launch_argv(self):
        # observe says the unit ended, then processes_gone raises. That record
        # used to chain the launch error, whose text is the argv.
        store = self.store()
        manager = Scripted(store, observe='empty', processes=RuntimeError('attest failed'))
        daemon = core.Daemon(store, manager, lambda: 'boot-a')
        self.assert_launch_error(daemon, ['not-executed', '--api-key', MARKER])
        rendered = self.text_of(self.grab.records)
        self.assertIn('termination attestation for recursion-run-one.service failed', rendered)
        self.assertNotIn(MARKER, rendered)
        self.assertNotIn('During handling of the above exception', rendered)

    def test_a_nested_recorded_cgroup_read_does_not_log_the_launch_argv(self):
        store = self.store()
        group = '/old-manager/recursion-run-one.service'

        def plant():
            store.control_put('one', json.dumps({'cgroup': group}))

        manager = Scripted(
            store, observe='empty', processes=True,
            on_start=plant, live=RuntimeError('cgroup unread'))
        daemon = core.Daemon(store, manager, lambda: 'boot-a')
        self.assert_launch_error(daemon, ['not-executed', '--api-key', MARKER])
        rendered = self.text_of(self.grab.records)
        self.assertIn('read of the recorded cgroup of one failed', rendered)
        self.assertNotIn(MARKER, rendered)
        self.assertNotIn('During handling of the above exception', rendered)

    def test_an_unreadable_control_record_during_launch_failure_does_not_log_the_argv(self):
        store = self.store()

        def plant():
            # A cgroup that is not text. json.loads of a bad document hides its
            # context, so this is a plain ValueError from the identity read.
            store.control_put('one', json.dumps({'cgroup': 1}))

        manager = Scripted(store, observe='empty', processes=True, on_start=plant)
        daemon = core.Daemon(store, manager, lambda: 'boot-a')
        self.assert_launch_error(daemon, ['not-executed', '--api-key', MARKER])
        rendered = self.text_of(self.grab.records)
        self.assertIn('control record of one is unreadable; its cgroup was not walked', rendered)
        self.assertNotIn(MARKER, rendered)
        self.assertNotIn('During handling of the above exception', rendered)

    def test_a_base_exception_from_the_reconcile_still_raises_the_launch_error(self):
        store = self.store()
        manager = core.SystemdUserManager()

        def call(argv):
            if argv[0] == 'systemd-run':
                raise subprocess.CalledProcessError(1, argv)
            return ('LoadState=not-found\nActiveState=inactive\nMainPID=0\n'
                    'ControlGroup=\n')

        manager._call = call
        daemon = core.Daemon(store, manager, lambda: 'boot-a')
        with patch.object(store, 'state', side_effect=Boom('not an exception')):
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                daemon.start('one', ['not-executed', '--api-key', MARKER])
        self.assertIn(MARKER, str(caught.exception))
        self.assertNotIn(MARKER, self.text_of(self.grab.records))

    def test_a_raising_log_handler_does_not_replace_the_launch_error(self):
        store = self.store()
        manager = core.SystemdUserManager()

        def call(argv):
            if argv[0] == 'systemd-run':
                raise subprocess.CalledProcessError(1, argv)
            return ('LoadState=not-found\nActiveState=inactive\nMainPID=0\n'
                    'ControlGroup=\n')

        manager._call = call
        daemon = core.Daemon(store, manager, lambda: 'boot-a')

        class Raiser(logging.Handler):
            def emit(self, record):
                raise RuntimeError('handler broke')

        raiser = Raiser(level=logging.ERROR)
        core.log.addHandler(raiser)
        self.addCleanup(core.log.removeHandler, raiser)
        with patch.object(store, 'state',
                          side_effect=sqlite3.OperationalError('test disk full')):
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                daemon.start('one', ['not-executed', '--api-key', MARKER])
        self.assertIn(MARKER, str(caught.exception))


if __name__ == '__main__':
    unittest.main()
