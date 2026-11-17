"""Small independent regression tests; run under run.sh and capped.sh. No live engine."""
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
        env = {key: os.environ[key] for key in ("TMPDIR", "TEMP", "TMP", "XDG_RUNTIME_DIR") if key in os.environ}
        env.update(OPENCODE_DISABLE_AUTOUPDATE="1", RECURSION_FAKE_OPENCODE_FIXTURE="synthetic-v1-success.jsonl")
        for model in ("a/", "a b/c", "a/" + "x" * 255):
            with self.subTest(model=repr(model[:10]), length=len(model)):
                self.assertFalse(oc.model_ok(model))
                with self.assertRaises(ValueError):
                    oc.build_argv(model)
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


if __name__ == "__main__":
    unittest.main()
