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

class Defects(Fixture):
    def test_D13_portable_test_skips(self):
            specs={
                'test_child_env':{'AuthRouteTests':['test_env_refused_shows_a_detail_only_when_it_has_one']},
                'test_no_proxy_readings':{
                    'WhitespaceTests':['test_the_space_set_is_python_whitespace_and_javascript_whitespace'],
                    'RangeTests':['test_the_range_parser'], 'IdnaFoldTests':['test_the_fold'],
                    'ReadingTimeTests':['test_hostile_values_are_decided_in_bounded_time','test_ranges_cost_one_interval_each']},
                'test_no_proxy_glob':{'StarMatcherTimeTests':['test_no_proxy_bypass_is_fast_and_decides_as_before','test_many_star_entries_in_one_value_are_fast']}}
            for name,classes in specs.items():
                m=importlib.import_module('recursion_core.child_env.tests.'+name)
                source=inspect.getsource(m)
                # Re-evaluate actual decorators with nt selected, then exercise unittest dispatch.
                # os.name itself stays POSIX; this is a skip audit, not native Windows evidence.
                for cls,methods in classes.items():
                    node=next(n for n in ast.parse(source).body if isinstance(n,ast.ClassDef) and n.name==cls)
                    selected=type(cls+'Selected',(getattr(m,cls),),{})
                    scope=dict(vars(m),os=SimpleNamespace(name='nt'))
                    for decorator in reversed(node.decorator_list):
                        selected=eval(compile(ast.Expression(decorator),'<real test decorator>','eval'),scope)(selected)
                    result=unittest.TestResult()
                    unittest.TestSuite(selected(method) for method in methods).run(result)
                    with self.subTest(test_class=cls):
                        self.assertEqual(result.errors,[])
                        self.assertEqual(result.failures,[])
                        self.assertEqual(result.skipped,[],'portable assertions never entered under Windows decorators')

if __name__ == "__main__":
    unittest.main(verbosity=2)
