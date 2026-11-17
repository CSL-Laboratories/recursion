"""The engine as a direct process (``python -m recursion_core.loop``), as a driver will run it: the fake
provider's conformance path, and the Anthropic client replaying recorded streams from a loopback server.

Each test writes its inputs, closes standard input and reads every output line; nothing waits on a clock.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from recursion_core.loop.tests.support import FAKE_KEY, POSIX, Loopback, private_dir, sse

CORE = Path(__file__).resolve().parents[3]


def run_engine(case, args: list[str], inputs: list[dict], workspace: Path, extra_env: dict | None = None,
               ) -> tuple[list[dict], str, int]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(CORE), "LC_ALL": "C.UTF-8"}
    env.update(extra_env or {})
    stdin = "".join(json.dumps(i) + "\n" for i in inputs)
    proc = subprocess.run([sys.executable, "-B", "-m", "recursion_core.loop", *args], input=stdin.encode(),
                          capture_output=True, env=env, cwd=workspace, check=False)
    out = proc.stdout.decode("utf-8")
    docs = [json.loads(line) for line in out.splitlines()]
    return docs, out + proc.stderr.decode("utf-8", "replace"), proc.returncode


def user(turn: str, text: str, delivery: str) -> dict:
    return {"kind": "user_message", "turn_id": turn, "text": text, "delivery_id": delivery}


class Keys(unittest.TestCase):
    def test_keys_leave_the_environment_and_go_to_the_redactor(self) -> None:
        from recursion_core.loop import __main__ as entry
        env = {"ANTHROPIC_API_KEY": FAKE_KEY, "OPENAI_API_KEY": "sk-openai-made-up-0001", "PATH": "/usr/bin",
               "ANTHROPIC_BASE_URL": "https://api.anthropic.com"}
        self.assertEqual(entry.take_keys(env), [FAKE_KEY, "sk-openai-made-up-0001"])
        self.assertEqual(env, {"PATH": "/usr/bin", "ANTHROPIC_BASE_URL": "https://api.anthropic.com"})


@unittest.skipUnless(POSIX, "the engine runs in a Linux run unit")
class FakeProviderProcess(unittest.TestCase):
    def test_a_read_and_an_answer_over_stdin_and_stdout(self) -> None:
        ws = private_dir(self, "loop-ws-")
        (ws / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")
        home = private_dir(self, "loop-home-") / "engine"
        script = ws.parent / f"{ws.name}-script.json"
        script.write_text(json.dumps([
            {"tool_calls": [{"id": "toolu_1", "name": "read", "input": {"path": "notes.txt"}}],
             "usage": {"input_tokens": 50, "output_tokens": 10}},
            {"text": "Two lines: alpha and beta.", "usage": {"input_tokens": 70, "output_tokens": 8}},
        ]), encoding="utf-8")
        self.addCleanup(script.unlink)
        docs, log, code = run_engine(self, ["--provider", "fake", "--fake-script", str(script), "--home", str(home)],
                                     [user("turn-1", "What is in notes.txt?", "dlv-1"), {"kind": "close"}], ws)
        self.assertEqual(code, 0, log)
        self.assertEqual([d["type"] for d in docs], ["ready", "input_accepted", "usage", "tool_call", "tool_result",
                                                     "usage", "text", "usage", "usage", "turn_ended", "closed"])
        self.assertEqual(docs[0]["tools"], ["edit", "read", "search", "shell", "write"])
        self.assertEqual((docs[0]["permissions"], docs[0]["acceptance"], docs[0]["instructions"]), ("ask", None, []))
        self.assertEqual(docs[4]["text"], "     1\talpha\n     2\tbeta\n")
        self.assertEqual(docs[9]["outcome"], "candidate_done")
        self.assertEqual(oct(os.stat(home).st_mode & 0o777), "0o700", "the engine home is created private")

    def test_a_key_in_the_environment_never_reaches_stdout_or_stderr(self) -> None:
        # The key is in the environment (as the child environment will put it) and in a file the model reads
        # and searches; the model also echoes it. None of it may come out. (The spill folder's side of this
        # is test_registry.Cap.test_secrets_are_removed_before_the_model_or_the_spill_sees_them: read and
        # search page themselves under the cap, so they never spill.)
        ws = private_dir(self, "loop-ws-")
        (ws / ".env").write_text(f"ANTHROPIC_API_KEY={FAKE_KEY}\n" + "padding line\n" * 300, encoding="utf-8")
        home = private_dir(self, "loop-home-")
        script = ws.parent / f"{ws.name}-script.json"
        script.write_text(json.dumps([
            {"tool_calls": [{"id": "toolu_1", "name": "search", "input": {"pattern": "API_KEY"}},
                            {"id": "toolu_2", "name": "read", "input": {"path": ".env", "limit": 3}}]},
            {"text": f"The key is {FAKE_KEY}."},
        ]), encoding="utf-8")
        self.addCleanup(script.unlink)
        docs, log, code = run_engine(self, ["--provider", "fake", "--fake-script", str(script), "--home", str(home)],
                                     [user("turn-1", "look", "dlv-1"), {"kind": "close"}], ws,
                                     {"ANTHROPIC_API_KEY": FAKE_KEY})
        self.assertEqual(code, 0, log)
        self.assertNotIn(FAKE_KEY, log)
        results = [d["text"] for d in docs if d["type"] == "tool_result"]
        self.assertEqual(results[0], ".env:1:ANTHROPIC_API_KEY=[redacted]\n")
        self.assertTrue(results[1].startswith("     1\tANTHROPIC_API_KEY=[redacted]\n"))
        self.assertEqual([d["text"] for d in docs if d["type"] == "text"], ["The key is [redacted]."])
        for path in (home / "spill").rglob("*") if (home / "spill").exists() else ():
            if path.is_file():
                self.assertNotIn(FAKE_KEY, path.read_text(encoding="utf-8"))

    def test_full_access_needs_its_acknowledgement_and_instructions_are_listed(self) -> None:
        ws = private_dir(self, "loop-ws-")
        (ws / "AGENTS.md").write_text("Run the tests with make test.\n", encoding="utf-8")
        home = private_dir(self, "loop-home-")
        script = ws.parent / f"{ws.name}-script.json"
        script.write_text("[]", encoding="utf-8")
        self.addCleanup(script.unlink)
        base = ["--provider", "fake", "--fake-script", str(script), "--home", str(home)]
        docs, log, code = run_engine(self, base + ["--permissions", "full_access"], [], ws)
        self.assertNotEqual(code, 0)
        self.assertIn("needs --acknowledge-full-access", log)
        self.assertEqual(docs, [])
        docs, log, code = run_engine(self, base + ["--permissions", "full_access", "--acknowledge-full-access",
                                                   "--acceptance", "make test"], [{"kind": "close"}], ws)
        self.assertEqual(code, 0, log)
        self.assertEqual((docs[0]["permissions"], docs[0]["acceptance"]), ("full_access", "make test"))
        self.assertEqual(docs[0]["instructions"], [{"path": "AGENTS.md", "bytes": 30, "cut": False, "skipped": None}])
        docs, _, _ = run_engine(self, base + ["--no-project-instructions"], [{"kind": "close"}], ws)
        self.assertEqual(docs[0]["instructions"], [])

    def test_a_missing_model_or_a_shared_home_is_refused_before_anything_runs(self) -> None:
        ws = private_dir(self, "loop-ws-")
        home = private_dir(self, "loop-home-")
        docs, log, code = run_engine(self, ["--provider", "anthropic", "--home", str(home)], [], ws,
                                     {"ANTHROPIC_API_KEY": FAKE_KEY})
        self.assertNotEqual(code, 0)
        self.assertIn("--model is required", log)
        self.assertEqual(docs, [])
        os.chmod(home, 0o755)
        script = ws.parent / f"{ws.name}-script.json"
        script.write_text("[]", encoding="utf-8")
        self.addCleanup(script.unlink)
        docs, log, code = run_engine(self, ["--provider", "fake", "--fake-script", str(script), "--home", str(home)],
                                     [], ws)
        self.assertNotEqual(code, 0)
        self.assertIn("open to other users", log)


@unittest.skipUnless(POSIX, "the engine runs in a Linux run unit")
class AnthropicReplayProcess(unittest.TestCase):
    def test_recorded_streams_drive_a_read_and_an_answer(self) -> None:
        ws = private_dir(self, "loop-ws-")
        (ws / "tests").mkdir()
        (ws / "tests" / "test_io.py").write_text("import io\n\ndef test_read():\n    assert io\n", encoding="utf-8")
        home = private_dir(self, "loop-home-")
        lb = Loopback(self, [sse("tool_use.sse"), sse("text_only.sse")])
        docs, log, code = run_engine(
            self, ["--provider", "anthropic", "--model", "claude-opus-5-5", "--home", str(home)],
            [user("turn-1", "Why does the test fail?", "dlv-7"), {"kind": "close"}], ws,
            {"ANTHROPIC_API_KEY": FAKE_KEY, "ANTHROPIC_BASE_URL": lb.url})
        self.assertEqual(code, 0, log)
        self.assertNotIn(FAKE_KEY, log)
        self.assertEqual([d["type"] for d in docs], ["ready", "input_accepted", "usage", "text", "tool_call",
                                                     "tool_result", "usage", "text", "usage", "usage", "turn_ended",
                                                     "closed"])
        self.assertEqual(docs[1], {"type": "input_accepted", "delivery_id": "dlv-7", "turn_id": "turn-1",
                                   "how": "new_turn"})
        self.assertEqual(docs[2]["model"], "claude-opus-5-5")
        self.assertEqual(docs[2]["cache_write_tokens"]["value"], 1500)
        self.assertEqual(docs[4]["input"], {"path": "tests/test_io.py", "limit": 50})
        self.assertTrue(docs[5]["text"].startswith("     1\timport io\n"))
        self.assertEqual(docs[10]["outcome"], "candidate_done")
        # What reached the server: the key in its header only, the thinking block sent back unchanged.
        self.assertEqual([r["headers"]["x-api-key"] for r in lb.requests], [FAKE_KEY, FAKE_KEY])
        for got in lb.requests:
            self.assertNotIn(FAKE_KEY, json.dumps(got["body"]))
        second = lb.requests[1]["body"]["messages"]
        self.assertEqual(second[1]["content"][0]["signature"], "c2lnbmF0dXJlLWZpeHR1cmUtMDE=")
        self.assertEqual(second[2]["content"][0]["tool_use_id"], "toolu_fixture_01")


if __name__ == "__main__":
    unittest.main()
