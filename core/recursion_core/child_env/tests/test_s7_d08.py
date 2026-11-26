import ast

from collections.abc import Mapping

import dataclasses

import errno

import importlib

import inspect

import os

from pathlib import Path

import signal

import stat

import sys

import tempfile

import threading

import time

from types import SimpleNamespace

import unittest

from unittest import mock

from recursion_core.child_env import ChildRoute, EnvRefused, build_child_env, policy, spawn


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='s7-child-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        self.home.mkdir(mode=0o700)
        self.binary = self.root / 'engine'
        self.binary.write_text('#!/bin/sh\nexit 0\n', encoding='ascii')
        self.binary.chmod(0o700)
        self.bases = mock.patch.object(policy, '_tmp_bases', return_value=[str(self.root)])
        self.bases.start()
        self.addCleanup(self.bases.stop)
        self.cache = mock.patch.dict(policy._TMP, {}, clear=True)
        self.cache.start()
        self.addCleanup(self.cache.stop)

    def build(self, **kw):
        return build_child_env(kw.pop('engine', 'claude_code'), home=kw.pop('home', str(self.home)),
                               auth_mode=kw.pop('auth_mode', 'subscription'), parent=kw.pop('parent', {}), **kw)

    def interleave(self, function, source_line, mutate, call):
        """Select one ordinary-container interleaving; no scheduling sleeps."""
        lines, first = inspect.getsourcelines(function)
        target = next(first+i for i,s in enumerate(lines) if s.strip() == source_line)
        entered, done = threading.Event(), threading.Event()
        errors = []
        def worker():
            try:
                if not entered.wait(3):
                    raise AssertionError('interleaving point was not reached')
                mutate()
            except BaseException as e:
                errors.append(e)
            finally:
                done.set()
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        fired = False
        def trace(frame, event, arg):
            nonlocal fired
            if not fired and event == 'line' and frame.f_code is function.__code__ and frame.f_lineno == target:
                fired = True
                entered.set()
                if not done.wait(3):
                    raise AssertionError('mutator missed bounded deadline')
            return trace
        previous = sys.gettrace()
        try:
            sys.settrace(trace)
            return call()
        finally:
            sys.settrace(previous)
            entered.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(fired)

def _gate_run_timeout() -> int:
    """The per-file limit the gate charges, read from its source. The gate is not run."""
    root = Path(__file__).resolve().parents[4]
    tree = ast.parse((root / "tools" / "ci_gate.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "RUN_TIMEOUT" and isinstance(node.value, ast.Constant):
                return int(node.value.value)
    raise AssertionError("RUN_TIMEOUT is not assigned in tools/ci_gate.py")


class Defects(Fixture):
    def test_D08_retry_budget(self):
            module=importlib.import_module('recursion_core.child_env.tests.test_no_proxy_readings')
            tree=ast.parse(inspect.getsource(module.ReadingTimeTests))
            cls=tree.body[0]
            method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_timed')
            hostile=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='test_hostile_values_are_decided_in_bounded_time')
            cases=next(n for n in ast.walk(hostile) if isinstance(n,ast.For) and isinstance(n.iter,ast.Tuple))
            count=len(cases.iter.elts)
            self.assertEqual(module.SECONDS, 10.0)
            self.assertEqual(module.GROWTH, 3.0)
            self.assertEqual(module.GROWTH_FLOOR_S, 0.05)
            limit = _gate_run_timeout()
            class Clock:
                now=0.0
                start=True
                durations=iter([d for _ in range(count) for d in (10.1,10.1,0.1,0.1)])
                def perf_counter(self):
                    if self.start:
                        self.start=False
                    else:
                        self.now+=next(self.durations)
                        self.start=True
                    return self.now
            clock=Clock()
            ns={'time':clock, 'SECONDS':module.SECONDS}
            exec(compile(ast.Module(body=[method],type_ignores=[]),'<source _timed>','exec'),ns)
            for _ in range(count):
                _, full=ns['_timed'](None,lambda:None)
                _, half=ns['_timed'](None,lambda:None)
                self.assertLess(full,module.SECONDS)
                self.assertLess(full,module.GROWTH*max(half,module.GROWTH_FLOOR_S))
            self.assertLessEqual(clock.now, limit, f'{count} passing pairs charge {clock.now:.1f} logical seconds to one file')

if __name__ == "__main__":
    unittest.main(verbosity=2)
