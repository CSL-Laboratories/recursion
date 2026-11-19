"""The fake ``opencode`` replays each SYNTHETIC fixture through a real pipe, and the parser reads it back (ENGINE-E3).

The fake is ``fake_opencode.py`` beside this file; it is not OpenCode and runs no model. It is started with the argv
``opencode.build_argv`` makes, the prompt on standard input and the child environment the lane asks for, so these
tests show the line splitting, the exit status and the argv together. Run by module name:

    PYTHONPATH=core <python> -m unittest recursion_core.routes.engines.fixtures.opencode.test_opencode_replay
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from recursion_core.routes.engines import opencode as oc

HERE = Path(__file__).resolve().parent
FAKE = HERE / "fake_opencode.py"
ENTRIES = {entry["file"]: entry for entry in json.loads((HERE / "index.json").read_text("utf-8"))["fixtures"]}
MODEL = "openai/gpt-5.5"


def run_fake(argv: list[str], env: dict[str, str], prompt: bytes = b"Run the tests.") -> subprocess.CompletedProcess:
    # argv[0] would be the binary; the fake is started by this interpreter instead, in isolated mode.
    return subprocess.run([sys.executable, "-I", "-B", str(FAKE), *argv[1:]], input=prompt, env=env,
                          capture_output=True, check=False)


def drafts_of(stdout: bytes, exit_code: int) -> tuple[oc.Parser, list[dict]]:
    p = oc.Parser(connection_id="subscription:chatgpt-plan", model=MODEL, wall=lambda: 1794614400.0,
                  clock=lambda: 100.0)
    out: list[dict] = []
    for line in stdout.splitlines(keepends=True):
        out.extend(p.feed(line))
    out.extend(p.end(exit_code))
    return p, out


class ReplayTests(unittest.TestCase):
    def env(self, name: str) -> dict[str, str]:
        return dict(oc.CHILD_ENV, RECURSION_FAKE_OPENCODE_FIXTURE=name)

    def test_each_fixture_comes_back_through_the_pipe_as_the_same_drafts(self):
        for name, entry in ENTRIES.items():
            with self.subTest(name):
                done = run_fake(oc.build_argv(MODEL), self.env(name))
                self.assertEqual(done.returncode, entry["exit_code"], done.stderr)
                self.assertEqual(done.stdout, (HERE / name).read_bytes())
                piped, drafts = drafts_of(done.stdout, done.returncode)
                direct, expected = drafts_of((HERE / name).read_bytes(), entry["exit_code"])
                self.assertEqual(drafts, expected)
                self.assertEqual([d["type"] for d in drafts], entry["expect"]["types"])
                self.assertEqual(piped.route_result("r1"), direct.route_result("r1"))

    def test_the_fake_refuses_an_argv_the_driver_would_not_build(self):
        name = "synthetic-v1-success.jsonl"
        for argv in (["opencode", "run", "--model", MODEL], ["opencode", "run", "--format", "json", "--model", "-x"],
                     ["opencode", "run", "--format", "json", "--model", MODEL, "the prompt"],
                     ["opencode", "serve"]):
            with self.subTest(argv):
                done = run_fake(argv, self.env(name))
                self.assertEqual((done.returncode, done.stdout), (2, b""))

    def test_the_fake_refuses_a_child_without_the_updater_off(self):
        done = run_fake(oc.build_argv(MODEL), {"RECURSION_FAKE_OPENCODE_FIXTURE": "synthetic-v1-success.jsonl"})
        self.assertEqual((done.returncode, done.stdout), (3, b""))

    def test_the_fake_refuses_an_empty_prompt_and_an_unknown_fixture(self):
        done = run_fake(oc.build_argv(MODEL), self.env("synthetic-v1-success.jsonl"), prompt=b"  \n")
        self.assertEqual((done.returncode, done.stdout), (1, b""))
        done = run_fake(oc.build_argv(MODEL), self.env("../index.json"))
        self.assertEqual((done.returncode, done.stdout), (2, b""))


if __name__ == "__main__":
    unittest.main()
