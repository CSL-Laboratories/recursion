"""Proposed missing tests for daemon/__init__.py and daemon/__main__.py.

Expected to fail at 7cde3ec3943640018dbe833093a712dd990b2d86.
All host manager calls are replaced. No plugin loading or credentials.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import errno
import io
import json
import logging
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import recursion_core.daemon as core
from recursion_core.daemon import __main__ as entry


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux daemon")
class S2BRegressions(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="s2b-regression-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_F7_engine_user_refusal_escapes_control_characters(self):
        with self.assertRaises(RuntimeError) as raised:
            core.refuse_engine_user({"RECURSION_ENGINE_USER": "operator\nERROR forged-entry\x1b[2J"})
        self.assertNotIn("\n", str(raised.exception))
        self.assertNotIn("\x1b", str(raised.exception))

if __name__ == "__main__":
    unittest.main(verbosity=2)
