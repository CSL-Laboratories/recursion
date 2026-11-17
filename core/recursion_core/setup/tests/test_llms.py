"""``llms.txt`` and ``recursiond mcp-config``.

``llms.txt`` is absent on the base, so the parse test fails before any command is built.
Once the file exists, every step must name a real ``recursiond`` subcommand and name the
JSON that command prints (a ``code``, the ``doctor_report`` object, or the Control MCP
snippet). ``mcp-config --client codex`` prints ``codex_operator_snippet`` and nothing else.
The command writes no client config.
"""

from __future__ import annotations

import contextlib
import inspect
import io
import json
import os
import re
import sys
import unittest
from unittest import mock

from recursion_core import _entrypoints as entry
from recursion_core.control.codex_config import codex_operator_snippet
from recursion_core.control.commands import setup
from recursion_core.setup import up

# core/recursion_core/setup/tests/<this file> -> repository root
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))))
_STEP = re.compile(r"^- `recursiond (?P<argv>[^`]+)` -> (?P<output>\S.*)$")
# #395's stdio server: ``python -m recursion_core.control.mcp`` takes no arguments.
# ``server.py`` names the server ``recursion-control``.
_SERVER_ID = "recursion-control"
_MCP_ARGS = ["-m", "recursion_core.control.mcp"]
_CLIENT_DIRS = (".codex", ".claude", ".cursor")
_WATCH = False
_OPENS = []


def _audit(event, args):
    if _WATCH and event == "open" and args:
        _OPENS.append(args[0])


sys.addaudithook(_audit)


def _main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = entry.main(argv)
    return code, out.getvalue(), err.getvalue()


def _steps(text):
    found = []
    for line in text.splitlines():
        match = _STEP.match(line)
        if match:
            found.append((match.group("argv").strip(), match.group("output").strip()))
        elif line.startswith("- `recursiond"):
            raise AssertionError(f"a step does not name its JSON output: {line}")
    return found


def _required():
    gui = json.dumps({"code": "ok", "gui": up.GUI}, separators=(",", ":"))
    return (
        ("doctor --json", "doctor_report"),
        ("provision", '{"code":"ok"}'),
        ("up", gui),
        ("mcp-config --client codex", "snippet"),
        ("mcp-config --client claude", "snippet"),
        ("mcp-config --client cursor", "snippet"),
    )


def _subcommand(argv):
    return argv.split()[0]


def _text(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class LlmsTxtTests(unittest.TestCase):
    def test_llms_txt_parses_and_each_command_is_a_subcommand_that_names_json(self):
        path = os.path.join(_ROOT, "llms.txt")
        self.assertTrue(os.path.isfile(path), "llms.txt is absent")
        steps = _steps(_text(path))
        self.assertGreaterEqual(len(steps), len(_required()))
        required = list(_required())
        at = 0
        for argv, output in steps:
            self.assertTrue(output, argv)
            name = _subcommand(argv)
            self.assertIn(name, {row[0] for row in entry.SUBCOMMANDS} | set(setup.NAMES) | {"serve"}, name)
            with mock.patch.object(entry, "_linux", return_value=True):
                code, help_out, help_err = _main([name, "--help"])
            self.assertEqual(code, 0, help_err or help_out)
            self.assertIn(name, help_out)
            if output == "doctor_report":
                self.assertEqual(argv, "doctor --json")
            elif output == "snippet":
                self.assertEqual(name, "mcp-config")
            else:
                payload = json.loads(output)
                self.assertIsInstance(payload.get("code"), str)
                self.assertTrue(payload["code"])
            if at < len(required) and (argv, output) == required[at]:
                at += 1
        self.assertEqual(at, len(required), "the install steps are missing or out of order")

    def test_named_json_is_what_the_command_prints(self):
        path = os.path.join(_ROOT, "llms.txt")
        self.assertTrue(os.path.isfile(path), "llms.txt is absent")
        named = dict(_steps(_text(path)))
        report = {"generated_at": "2026-11-17T00:00:00.000Z", "checks": []}
        with mock.patch.object(entry, "_linux", return_value=True), \
                mock.patch("recursion_core.setup.doctor.run", return_value=report):
            code, out, err = _main(["doctor", "--json"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), report)
        self.assertEqual(named["doctor --json"], "doctor_report")

        self.assertEqual(json.loads(named["provision"])["code"], "ok")
        self.assertIn('"code": "ok"', inspect.getsource(setup.provision))

        buf = io.StringIO()
        with mock.patch("recursion_core.setup.up.socket_path", return_value="/no/such.sock"):
            status = up.command(out=buf, runner=lambda args: 0, report={"checks": []},
                                socket_wait=lambda path, bound: True, environ={})
        printed = json.loads(buf.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(printed["code"], json.loads(named["up"])["code"])
        self.assertEqual(printed["gui"], json.loads(named["up"])["gui"])
        self.assertEqual(printed["gui"], up.GUI)


class McpConfigTests(unittest.TestCase):
    def test_codex_prints_the_operator_snippet(self):
        code, out, err = _main(["mcp-config", "--client", "codex"])
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertEqual(out, codex_operator_snippet(_SERVER_ID, sys.executable, _MCP_ARGS))
        again, again_out, again_err = _main(["mcp-config", "--client", "codex"])
        self.assertEqual((again, again_out, again_err), (code, out, err))

    def test_claude_and_cursor_print_one_json_snippet(self):
        for client in ("claude", "cursor"):
            with self.subTest(client=client):
                code, out, err = _main(["mcp-config", "--client", client])
                self.assertEqual((code, err), (0, ""))
                parsed = json.loads(out)
                server = parsed["mcpServers"][_SERVER_ID]
                self.assertEqual(server["command"], sys.executable)
                self.assertEqual(server["args"], _MCP_ARGS)
                self.assertEqual(set(parsed), {"mcpServers"})
                self.assertEqual(set(server), {"command", "args"})
                self.assertEqual(out, json.dumps(parsed, sort_keys=True) + "\n")

    def test_a_second_client_call_prints_the_same_snippet(self):
        first = _main(["mcp-config", "--client", "cursor"])
        second = _main(["mcp-config", "--client", "cursor"])
        self.assertEqual(first, second)
        self.assertEqual(first[0], 0)

    def test_the_snippet_is_not_written_into_a_client_config(self):
        global _WATCH
        _OPENS.clear()
        _WATCH = True
        try:
            for client in ("codex", "claude", "cursor"):
                code, _out, _err = _main(["mcp-config", "--client", client])
                self.assertEqual(code, 0)
        finally:
            _WATCH = False
        for raw in _OPENS:
            text = raw if isinstance(raw, str) else os.fsdecode(raw)
            parts = text.replace("\\", "/").split("/")
            self.assertFalse(any(part in _CLIENT_DIRS for part in parts), text)

    def test_a_missing_or_unknown_client_is_a_usage_error(self):
        with mock.patch.object(entry, "_linux", return_value=True):
            code, out, err = _main(["mcp-config"])
            self.assertEqual((code, out), (2, ""))
            self.assertIn("usage error", err)
            code, out, err = _main(["mcp-config", "--client", "other"])
            self.assertEqual((code, out), (2, ""))
            self.assertIn("usage error", err)

    def test_off_linux_refuses_before_a_snippet_is_printed(self):
        with mock.patch.object(entry, "_linux", return_value=False), \
                mock.patch("recursion_core.control.codex_config.codex_operator_snippet") as snippet:
            code, out, _err = _main(["mcp-config", "--client", "codex"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["code"], "platform_unsupported")
        snippet.assert_not_called()

    def test_an_interpreter_without_an_absolute_path_is_refused(self):
        with mock.patch.object(entry, "_linux", return_value=True), \
                mock.patch.object(entry.sys, "executable", "bin/python"):
            code, out, _err = _main(["mcp-config", "--client", "claude"])
        self.assertEqual((code, json.loads(out)["code"]), (1, "interpreter_unknown"))

    def test_an_interrupt_propagates_and_a_snippet_error_is_not_swallowed(self):
        with mock.patch.object(entry, "_linux", return_value=True), \
                mock.patch("recursion_core.control.codex_config.codex_operator_snippet",
                           side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                _main(["mcp-config", "--client", "codex"])
        with mock.patch.object(entry, "_linux", return_value=True), \
                mock.patch("recursion_core.control.codex_config.codex_operator_snippet",
                           side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                _main(["mcp-config", "--client", "codex"])

    def test_help_lists_the_command_and_the_three_clients(self):
        code, out, _err = _main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("mcp-config", out)
        with mock.patch.object(entry, "_linux", return_value=True):
            code, out, _err = _main(["mcp-config", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("--client", out)
        for client in ("codex", "claude", "cursor"):
            self.assertIn(client, out)


class AgentInstallPageTests(unittest.TestCase):
    def test_the_page_lists_every_llms_step_and_says_the_snippet_is_not_written(self):
        page_path = os.path.join(_ROOT, "docs", "install", "AGENT-INSTALL.md")
        llms_path = os.path.join(_ROOT, "llms.txt")
        self.assertTrue(os.path.isfile(page_path), "docs/install/AGENT-INSTALL.md is absent")
        self.assertTrue(os.path.isfile(llms_path), "llms.txt is absent")
        page = _text(page_path)
        for argv, _output in _steps(_text(llms_path)):
            self.assertIn(f"recursiond {argv}", page)
        self.assertIn("does not write", page)
        for name in _CLIENT_DIRS:
            self.assertIn(name, page)


if __name__ == "__main__":
    unittest.main()
