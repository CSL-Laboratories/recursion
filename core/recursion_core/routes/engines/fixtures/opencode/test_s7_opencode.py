"""Small independent regression tests. No live engine.

Run from the worktree root:

    PYTHONPATH=core python -m unittest recursion_core.routes.engines.fixtures.opencode.test_s7_opencode
"""
import importlib.util
import json
import math
import os
import subprocess
import sys
import unittest
from fractions import Fraction
from pathlib import Path

from recursion_core.routes.engines import opencode as oc

WALL = 1794614400.0


def parser(**kwargs):
    args = dict(connection_id="api_key:synthetic", model="a/b", wall=lambda: WALL, clock=lambda: 100.0)
    args.update(kwargs)
    return oc.Parser(**args)


def line(value):
    return json.dumps(value).encode() + b"\n"


class FindingsTests(unittest.TestCase):
    def test_fake_refuses_models_the_builder_refuses(self):
        fake = Path(oc.__file__).parent / "fixtures/opencode/fake_opencode.py"
        spec = importlib.util.spec_from_file_location("fake_opencode_standin", fake)
        standin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(standin)
        env = {key: os.environ[key] for key in ("TMPDIR", "TEMP", "TMP", "XDG_RUNTIME_DIR") if key in os.environ}
        env.update(OPENCODE_DISABLE_AUTOUPDATE="1", RECURSION_FAKE_OPENCODE_FIXTURE="synthetic-v1-success.jsonl")
        # The four spellings after the three named ones hold the stand-in's other branches:
        # a leading dash, no slash, a control character, a lone surrogate.
        models = ("a/", "a b/c", "a/" + "x" * 255, "-a/b", "ab", "a/\x01b", "a/\ud800")
        for model in models:
            with self.subTest(model=repr(model[:12]), length=len(model)):
                self.assertFalse(oc.model_ok(model))
                self.assertFalse(standin._model_ok(model))
                with self.assertRaises(ValueError):
                    oc.build_argv(model)
                # A lone surrogate cannot cross this host's argv encoding. The builder's rule
                # above is what refuses it; the stand-in is checked as a process for the rest.
                if any(0xD800 <= ord(ch) <= 0xDFFF for ch in model):
                    continue
                done = subprocess.run(
                    [sys.executable, "-I", "-B", str(fake), "run", "--format", "json", "--model", model],
                    input=b"synthetic prompt", env=env, capture_output=True, timeout=5)
                self.assertEqual((done.returncode, done.stdout), (2, b""))

    def test_reset_rounds_up_without_an_intermediate_float_round_down(self):
        # Real float is about 93 ns later than the exact decimal millisecond boundary.
        now = WALL + .002
        p = parser(wall=lambda: now)
        ds = p.feed(line({"type": "error", "error": {"type": "provider.rate-limit", "status": 429,
                                                     "retryAfterMs": 1000}}))
        reset = ds[-1]["payload"]["reset_at"]
        target_ms = math.ceil(Fraction.from_float(now + 1) * 1000)
        self.assertEqual(target_ms, 1794614401003)
        self.assertEqual(reset, "2026-11-14T00:00:01.003Z")

    def test_a_reset_at_the_millisecond_boundary_is_not_earlier_than_the_exact_wait(self):
        # now + retryAfterMs/1000 as a float is one millisecond under the exact sum.
        now, retry_ms = 1714257175.493, 14590
        exact = Fraction(now) + Fraction(retry_ms, 1000)
        floated = Fraction(now + retry_ms / 1000.0)
        self.assertEqual(math.ceil(exact * 1000), math.ceil(floated * 1000) + 1)
        p = parser(wall=lambda: now)
        ds = p.feed(line({"type": "error", "error": {"type": "provider.rate-limit", "status": 429,
                                                     "retryAfterMs": retry_ms}}))
        self.assertEqual(ds[-1]["payload"]["reset_at"], "2024-04-27T22:33:10.084Z")

    def test_the_module_docstring_names_no_missing_runner(self):
        self.assertNotIn("run.sh", __doc__ or "")


if __name__ == "__main__":
    unittest.main()
