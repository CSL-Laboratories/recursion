"""Witnesses for a copy that fails, an override read that fails, and a non-dict mapping.

A list whose iteration raises, or a mapping whose items() raises or yields a
non-pair, is a typed refusal. A mapping that is not a dict is applied from one
copy, so a second items() that is empty or reordered is not a change.
KeyboardInterrupt still propagates.
"""
import os
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest import mock

from recursion_core.child_env import ChildRoute, EnvRefused, build_child_env, policy


class _Items(Mapping):
    def __init__(self, rows, error=None):
        self._rows = list(rows)
        self._error = error

    def __getitem__(self, key):
        for name, value in self._pairs():
            if name == key:
                return value
        raise KeyError(key)

    def __iter__(self):
        return iter(name for name, _value in self._pairs())

    def __len__(self):
        return len(self._pairs())

    def _pairs(self):
        return [(row[0], row[1]) for row in self._rows]

    def items(self):
        if self._error is not None:
            raise self._error
        return iter(self._rows)


class _Once(Mapping):
    def __init__(self, rows):
        self._rows = list(rows)
        self.calls = 0

    def __getitem__(self, key):
        for name, value in self._rows:
            if name == key:
                return value
        raise KeyError(key)

    def __iter__(self):
        return iter(name for name, _value in self._rows)

    def __len__(self):
        return len(self._rows)

    def items(self):
        self.calls += 1
        if self.calls == 1:
            return list(self._rows)
        return []


class _Flip(Mapping):
    def __init__(self, rows):
        self._rows = list(rows)
        self.calls = 0

    def __getitem__(self, key):
        for name, value in self._rows:
            if name == key:
                return value
        raise KeyError(key)

    def __iter__(self):
        return iter(name for name, _value in self._rows)

    def __len__(self):
        return len(self._rows)

    def items(self):
        self.calls += 1
        rows = list(self._rows)
        if self.calls % 2 == 0:
            rows.reverse()
        return rows


class _BoomList(list):
    def __init__(self, values, error):
        super().__init__(values)
        self._error = error

    def __iter__(self):
        raise self._error


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='s7-copy-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        self.home.mkdir(mode=0o700)
        self.bases = mock.patch.object(policy, '_tmp_bases', return_value=[str(self.root)])
        self.bases.start()
        self.addCleanup(self.bases.stop)
        self.cache = mock.patch.dict(policy._TMP, {}, clear=True)
        self.cache.start()
        self.addCleanup(self.cache.stop)

    def build(self, **kw):
        return build_child_env(
            kw.pop('engine', 'claude_code'),
            home=kw.pop('home', str(self.home)),
            auth_mode=kw.pop('auth_mode', 'subscription'),
            parent=kw.pop('parent', {}),
            **kw,
        )


@unittest.skipIf(os.name == "nt", "POSIX rules")
class CopyWindow(Fixture):
    def test_a_switch_list_whose_iteration_fails_is_route_malformed(self):
        for error in (KeyError('gone'), ValueError('gone'), OSError('gone')):
            with self.subTest(error=type(error).__name__):
                route = ChildRoute(provider_switches=_BoomList(['CLAUDE_CODE_USE_BEDROCK'], error))
                with self.assertRaises(EnvRefused) as caught:
                    self.build(auth_mode='cloud_provider', route=route)
                self.assertIsNone(caught.exception.name)
                self.assertEqual('route_malformed', caught.exception.cause)

    def test_a_switch_list_interrupt_propagates(self):
        route = ChildRoute(provider_switches=_BoomList(['CLAUDE_CODE_USE_BEDROCK'], KeyboardInterrupt()))
        with self.assertRaises(KeyboardInterrupt):
            self.build(auth_mode='cloud_provider', route=route)

    def test_an_override_read_that_fails_is_override_malformed(self):
        for error in (KeyError('gone'), OSError('gone'), ValueError('gone')):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(EnvRefused) as caught:
                    self.build(overrides=_Items([('LANG', 'C')], error))
                self.assertIsNone(caught.exception.name)
                self.assertEqual('override_malformed', caught.exception.cause)

    def test_an_override_item_that_is_not_a_pair_is_override_malformed(self):
        with self.assertRaises(EnvRefused) as caught:
            self.build(overrides=_Items([('LANG', 'C', 'extra')]))
        self.assertIsNone(caught.exception.name)
        self.assertEqual('override_malformed', caught.exception.cause)

    def test_an_override_interrupt_propagates(self):
        with self.assertRaises(KeyboardInterrupt):
            self.build(overrides=_Items([('LANG', 'C')], KeyboardInterrupt()))

    def test_a_non_dict_mapping_is_applied_from_one_copy(self):
        once = _Once([('LANG', 'C')])
        env = self.build(overrides=once)
        self.assertEqual('C', env['LANG'])
        self.assertGreaterEqual(once.calls, 1)

        flip = _Flip([('LANG', 'C'), ('LC_CTYPE', 'C')])
        env = self.build(overrides=flip)
        self.assertEqual('C', env['LANG'])
        self.assertEqual('C', env['LC_CTYPE'])

    def test_a_dict_that_changes_after_the_copy_is_still_refused(self):
        # The second read stays for a dict. This is the same rule D06 drives with a trace.
        class Live(dict):
            def items(self):
                if not hasattr(self, '_n'):
                    self._n = 0
                self._n += 1
                if self._n == 1:
                    return [('LANG', 'C')]
                return [('LANG', 'C'), ('LC_ALL', 'C')]

        with self.assertRaises(EnvRefused) as caught:
            self.build(overrides=Live())
        self.assertEqual('override_malformed', caught.exception.cause)


if __name__ == "__main__":
    unittest.main(verbosity=2)
