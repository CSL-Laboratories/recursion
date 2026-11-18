"""Observe local tool setup without copying secret values.

Red on the doctor-core base: ``recursion_core.setup.observe`` is not there, so each test
errors on import. Once the module exists, a run is red when a canary planted in a sign-in
store, a settings ``env`` value, or an MCP ``env`` / ``headers`` value reaches stdout or
the record, when the audit hook sees a sign-in store opened, or when a mode-000
``config.toml`` is reported absent.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock

from recursion_core import _entrypoints as entry

_CANARY_AUTH = "CANARY-AUTH-7c1e9a4b-DO-NOT-LEAK"
_CANARY_CRED = "CANARY-CRED-7c1e9a4b-DO-NOT-LEAK"
_CANARY_ENV = "CANARY-ENV-7c1e9a4b-DO-NOT-LEAK"
_CANARY_MCP_ENV = "CANARY-MCPENV-7c1e9a4b-DO-NOT-LEAK"
_CANARY_MCP_HDR = "CANARY-MCPHDR-7c1e9a4b-DO-NOT-LEAK"
_CANARY_ARGS = "CANARY-ARGS-7c1e9a4b-DO-NOT-LEAK"
_CANARY_CODEX = "CANARY-CODEX-7c1e9a4b-DO-NOT-LEAK"
_CANARY_CURSOR_ENV = "CANARY-CURSENV-7c1e9a4b-DO-NOT-LEAK"
_CANARY_CURSOR_HDR = "CANARY-CURSHDR-7c1e9a4b-DO-NOT-LEAK"
_CANARY_GIT_NAME = "CANARY-GITNAME-7c1e9a4b-DO-NOT-LEAK"
_CANARY_GIT_MAIL = "CANARY-GITEMAIL-7c1e9a4b-DO-NOT-LEAK"
_CANARY_GIT_URL = "CANARY-GITURL-7c1e9a4b-DO-NOT-LEAK"
_CANARY_AGENTS = "CANARY-AGENTS-7c1e9a4b-DO-NOT-LEAK"
_CANARY_CLAUDE_MD = "CANARY-CLAUDEMD-7c1e9a4b-DO-NOT-LEAK"
_CANARY_SEALED = "CANARY-SEALED-7c1e9a4b-DO-NOT-LEAK"
_CANARY_RUNNER = "CANARY-RUNNER-7c1e9a4b-DO-NOT-LEAK"
_CANARY_CMD = "CANARY-CMD-7c1e9a4b-DO-NOT-LEAK"
_CANARY_LINK = "CANARY-LINK-7c1e9a4b-DO-NOT-LEAK"
_CANARIES = (
    _CANARY_AUTH, _CANARY_CRED, _CANARY_ENV, _CANARY_MCP_ENV, _CANARY_MCP_HDR, _CANARY_ARGS,
    _CANARY_CODEX, _CANARY_CURSOR_ENV, _CANARY_CURSOR_HDR, _CANARY_GIT_NAME, _CANARY_GIT_MAIL,
    _CANARY_GIT_URL, _CANARY_AGENTS, _CANARY_CLAUDE_MD, _CANARY_SEALED, _CANARY_RUNNER,
    _CANARY_CMD, _CANARY_LINK,
)
_OPENED: list = []
_HOOKED = False
_STAMP = (
    r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{3}Z$"
)


def _remember_open(event, args):
    if event == "open" and args:
        try:
            _OPENED.append(args[0])
        except Exception:
            return


def _watch_opens():
    global _HOOKED
    if not _HOOKED:
        sys.addaudithook(_remember_open)
        _HOOKED = True


def _observe():
    import recursion_core.setup.observe as observe
    return observe


def _write(path, text, mode=0o600):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, mode)


def _plant_denied(home):
    import recursion_core.setup.deny as deny

    for path in deny.denied_paths(home):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        leaf = os.path.basename(path)
        if leaf.startswith(".") or path.endswith(".json"):
            _write(path, _CANARY_AUTH + "\n" + _CANARY_CRED + "\n", 0o600)
        else:
            os.makedirs(path, exist_ok=True)
            _write(os.path.join(path, "secret.txt"), _CANARY_AUTH, 0o600)


def _login_ok(argv):
    if list(argv) == ["claude", "auth", "status"]:
        return 0
    if list(argv) == ["codex", "login", "status"]:
        return 1
    raise AssertionError(argv)


def _source(report, source_id):
    found = [row for row in report["sources"] if row["id"] == source_id]
    if len(found) != 1:
        raise AssertionError(f"{source_id}: {found!r}")
    return found[0]


def _by_path(report, path):
    found = [row for source in report["sources"] for row in source["files"] if row["path"] == path]
    if len(found) != 1:
        raise AssertionError(f"{path}: {found!r}")
    return found[0]


def _clean(blob):
    # The failure text stays plain: it must not repeat a planted marker.
    for canary in _CANARIES:
        if canary in blob:
            raise AssertionError("canary reached the output")


def _opened_names(opened):
    return [os.fsdecode(raw) if isinstance(raw, bytes) else str(raw) for raw in opened]


def _ends(opened, suffix):
    return any(name.endswith(suffix) for name in _opened_names(opened))


@contextlib.contextmanager
def _umask_open():
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


class ObserveTests(unittest.TestCase):
    def _layout(self):
        root = tempfile.TemporaryDirectory(prefix="observe-")
        self.addCleanup(root.cleanup)
        home = os.path.join(root.name, "home")
        project = os.path.join(root.name, "project")
        state = os.path.join(root.name, "state")
        os.mkdir(home, 0o700)
        os.mkdir(project, 0o700)
        os.mkdir(state, 0o700)
        return home, project, state

    def _run(self, home, project, state, runner=_login_ok):
        observe = _observe()
        _watch_opens()
        before = len(_OPENED)
        out = io.StringIO()
        with _umask_open():
            code = observe.command(out, home=home, project=project, state_home=state, runner=runner)
        text = out.getvalue()
        record = os.path.join(state, "recursion", "observe", "record.json")
        disk = b""
        if os.path.isfile(record):
            with open(record, "rb") as handle:
                disk = handle.read()
        return code, text, record, disk, _OPENED[before:]

    def _assert_private(self, home, opened, text, disk):
        import recursion_core.setup.deny as deny

        _clean(text)
        _clean(disk.decode("utf-8", "replace"))
        for raw in opened:
            self.assertFalse(deny.is_denied(raw, home), raw)
            shown = os.fsdecode(raw) if isinstance(raw, bytes) else str(raw)
            self.assertFalse(shown.endswith("auth.json"), shown)
            self.assertFalse(shown.endswith(".credentials.json"), shown)

    def test_canaries_stay_out_and_a_mode_000_config_is_unknown(self):
        observe = _observe()
        home, project, state = self._layout()
        _plant_denied(home)
        settings = {
            "model": "haiku-observe",
            "env": {"ANTHROPIC_API_KEY": _CANARY_ENV},
            "mcpServers": {
                "maps": {
                    "command": "map-tool",
                    "args": ["--flag", _CANARY_ARGS],
                    "env": {"TOKEN": _CANARY_MCP_ENV},
                    "headers": {"Authorization": _CANARY_MCP_HDR},
                }
            },
        }
        settings_path = os.path.join(home, ".claude", "settings.json")
        _write(settings_path, json.dumps(settings), 0o600)
        _write(os.path.join(home, ".claude.json"), json.dumps({"oauth": _CANARY_SEALED}), 0o600)
        toml = (
            f'model = "gpt-observe"\napproval_policy = "on-request"\n'
            f'sandbox_mode = "workspace-write"\nnote = "{_CANARY_CODEX}"\n'
        )
        config_path = os.path.join(home, ".codex", "config.toml")
        _write(config_path, toml, 0o600)
        os.chmod(config_path, 0o000)
        self.addCleanup(os.chmod, config_path, 0o600)
        cursor = {
            "mcpServers": {
                "draw": {
                    "command": "draw-tool",
                    "env": {"TOKEN": _CANARY_CURSOR_ENV},
                    "headers": {"Authorization": _CANARY_CURSOR_HDR},
                }
            }
        }
        _write(os.path.join(home, ".cursor", "mcp.json"), json.dumps(cursor), 0o600)
        gitconfig = (
            "[user]\n"
            f"    name = {_CANARY_GIT_NAME}\n"
            f"    email = {_CANARY_GIT_MAIL}\n"
            '[remote "origin"]\n'
            f"    url = https://{_CANARY_GIT_URL}@example.invalid/repo\n"
        )
        _write(os.path.join(home, ".gitconfig"), gitconfig, 0o644)
        agents = f"# instructions\n{_CANARY_AGENTS}\n"
        claude_md = f"# project\n{_CANARY_CLAUDE_MD}\n"
        _write(os.path.join(project, "AGENTS.md"), agents, 0o644)
        _write(os.path.join(project, "CLAUDE.md"), claude_md, 0o644)

        code, text, record, disk, opened = self._run(home, project, state)
        self.assertEqual(code, 0, text)
        self._assert_private(home, opened, text, disk)
        report = json.loads(text)
        self.assertEqual(disk.decode("ascii"), text)
        self.assertEqual(report["code"], "ok")
        self.assertRegex(report["generated_at"], _STAMP)
        self.assertEqual(report["record"], record)
        self.assertEqual(stat.S_IMODE(os.stat(record).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(record)).st_mode), 0o700)
        self.assertEqual(
            [row["id"] for row in report["sources"]],
            ["claude", "codex", "cursor", "git", "project"],
        )
        self.assertEqual(_source(report, "claude")["login_exit_code"], 0)
        self.assertEqual(_source(report, "codex")["login_exit_code"], 1)
        self.assertNotIn("login_exit_code", _source(report, "cursor"))
        self.assertNotIn("login_exit_code", _source(report, "git"))
        self.assertNotIn("login_exit_code", _source(report, "project"))

        seen = _by_path(report, settings_path)
        self.assertEqual(seen["state"], "ok")
        self.assertTrue(seen["exists"])
        self.assertEqual(seen["size"], os.path.getsize(settings_path))
        self.assertEqual(seen["sha256"], hashlib.sha256(json.dumps(settings).encode()).hexdigest())
        self.assertIs(seen["owner_only"], True)
        self.assertEqual(seen["allow"]["model"], "haiku-observe")
        self.assertEqual(seen["allow"]["mcp_servers"], [{"command": "map-tool", "name": "maps"}])
        self.assertNotIn("env", seen["allow"])
        self.assertNotIn("headers", json.dumps(seen["allow"]))

        sealed = _by_path(report, os.path.join(home, ".claude.json"))
        self.assertTrue(sealed["exists"])
        self.assertEqual(sealed["state"], "unknown")
        self.assertIsNone(sealed["sha256"])
        self.assertNotIn("allow", sealed)

        config = _by_path(report, config_path)
        self.assertTrue(config["exists"])
        self.assertEqual(config["state"], "unknown")
        self.assertNotEqual(config["state"], "absent")
        self.assertIsNone(config["sha256"])
        self.assertEqual(config["size"], len(toml.encode()))
        self.assertNotIn("allow", config)

        draw = _by_path(report, os.path.join(home, ".cursor", "mcp.json"))
        self.assertEqual(draw["state"], "ok")
        self.assertEqual(draw["allow"]["mcp_servers"], [{"command": "draw-tool", "name": "draw"}])

        git_row = _by_path(report, os.path.join(home, ".gitconfig"))
        self.assertEqual(git_row["state"], "ok")
        self.assertIs(git_row["owner_only"], False)
        self.assertEqual(git_row["allow"], {"user_email_present": True, "user_name_present": True})

        agents_row = _by_path(report, os.path.join(project, "AGENTS.md"))
        self.assertEqual(agents_row["state"], "ok")
        self.assertEqual(agents_row["sha256"], hashlib.sha256(agents.encode()).hexdigest())
        self.assertNotIn("allow", agents_row)
        claude_row = _by_path(report, os.path.join(project, "CLAUDE.md"))
        self.assertEqual(claude_row["sha256"], hashlib.sha256(claude_md.encode()).hexdigest())
        self.assertNotIn(_CANARY_AGENTS, text)
        self.assertNotIn(_CANARY_CLAUDE_MD, text)

        missing = _by_path(report, os.path.join(project, ".claude", "settings.json"))
        self.assertFalse(missing["exists"])
        self.assertEqual(missing["state"], "absent")
        self.assertIsNone(missing["size"])
        self.assertIsNone(missing["sha256"])
        self.assertIsNone(missing["owner_only"])

        paths = [row["path"] for source in report["sources"] for row in source["files"]]
        self.assertEqual(paths, [
            os.path.join(home, ".claude", "settings.json"),
            os.path.join(home, ".claude.json"),
            os.path.join(project, ".claude", "settings.json"),
            os.path.join(home, ".codex", "config.toml"),
            os.path.join(home, ".cursor", "mcp.json"),
            os.path.join(project, ".cursor", "mcp.json"),
            os.path.join(home, ".gitconfig"),
            os.path.join(home, ".config", "git", "config"),
            os.path.join(project, ".git", "config"),
            os.path.join(project, "AGENTS.md"),
            os.path.join(project, "CLAUDE.md"),
        ])
        self.assertIs(observe.run is not None, True)

    def test_a_readable_codex_config_keeps_model_approval_and_sandbox_only(self):
        home, project, state = self._layout()
        _plant_denied(home)
        toml = (
            'model = "gpt-observe"\n'
            'approval_policy = "on-request"\n'
            'sandbox_mode = "workspace-write"\n'
            "\n"
            "[mcp_servers.docs]\n"
            'command = "docs-tool"\n'
            f'env = {{ TOKEN = "{_CANARY_CODEX}" }}\n'
            f'http_headers = {{ Authorization = "{_CANARY_MCP_HDR}" }}\n'
        )
        config_path = os.path.join(home, ".codex", "config.toml")
        _write(config_path, toml, 0o600)
        code, text, _record, disk, opened = self._run(home, project, state)
        self.assertEqual(code, 0, text)
        self._assert_private(home, opened, text, disk)
        config = _by_path(json.loads(text), config_path)
        self.assertEqual(config["state"], "ok")
        self.assertEqual(config["allow"]["model"], "gpt-observe")
        self.assertEqual(config["allow"]["approval"], "on-request")
        self.assertEqual(config["allow"]["sandbox"], "workspace-write")
        self.assertEqual(config["allow"]["mcp_servers"], [{"command": "docs-tool", "name": "docs"}])
        self.assertIs(config["owner_only"], True)

    def test_a_symlink_or_hardlink_to_a_sign_in_store_is_not_opened(self):
        home, project, state = self._layout()
        _plant_denied(home)
        auth = os.path.join(home, ".codex", "auth.json")
        config = os.path.join(home, ".codex", "config.toml")
        os.symlink(auth, config)
        code, text, _record, disk, opened = self._run(home, project, state)
        self.assertEqual(code, 0, text)
        self._assert_private(home, opened, text, disk)
        row = _by_path(json.loads(text), config)
        self.assertTrue(row["exists"])
        self.assertEqual(row["state"], "unknown")
        self.assertIsNone(row["sha256"])
        shown = [os.fsdecode(raw) if isinstance(raw, bytes) else str(raw) for raw in opened]
        self.assertFalse(any(path.endswith("config.toml") for path in shown), shown)

        os.unlink(config)
        os.link(auth, config)
        code, text, _record, disk, opened = self._run(home, project, state)
        self.assertEqual(code, 0, text)
        self._assert_private(home, opened, text, disk)
        row = _by_path(json.loads(text), config)
        # A second name is an alias: one plain state, no hash and no field.
        self.assertEqual(row["state"], "aliased")
        self.assertIsNone(row["sha256"])
        self.assertNotIn("allow", row)
        shown = [os.fsdecode(raw) if isinstance(raw, bytes) else str(raw) for raw in opened]
        self.assertFalse(any(path.endswith("config.toml") or path.endswith("auth.json") for path in shown), shown)

    def test_login_keeps_an_exit_code_only_and_a_bad_runner_stays_unknown(self):
        home, project, state = self._layout()

        def leaks(argv):
            return (0, _CANARY_RUNNER)

        code, text, _record, disk, opened = self._run(home, project, state, runner=leaks)
        self.assertEqual(code, 0, text)
        self._assert_private(home, opened, text, disk)
        report = json.loads(text)
        self.assertIsNone(_source(report, "claude")["login_exit_code"])
        self.assertIsNone(_source(report, "codex")["login_exit_code"])

        def blows(argv):
            raise RuntimeError(_CANARY_RUNNER)

        code, text, _record, disk, opened = self._run(home, project, state, runner=blows)
        self.assertEqual(code, 0, text)
        self._assert_private(home, opened, text, disk)
        self.assertIsNone(_source(json.loads(text), "claude")["login_exit_code"])

    def test_interruption_propagates_and_a_second_caller_is_separate(self):
        observe = _observe()
        home, project, state = self._layout()
        _write(os.path.join(home, ".claude", "settings.json"), json.dumps({"model": "haiku-observe"}), 0o600)

        def stop(argv):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            observe.command(io.StringIO(), home=home, project=project, state_home=state, runner=stop)
        record = os.path.join(state, "recursion", "observe", "record.json")
        self.assertFalse(os.path.exists(record))

        def leave(argv):
            raise SystemExit(3)

        with self.assertRaises(SystemExit):
            observe.command(io.StringIO(), home=home, project=project, state_home=state, runner=leave)
        self.assertFalse(os.path.exists(record))

        first_out = io.StringIO()
        second_out = io.StringIO()
        self.assertEqual(observe.command(first_out, home=home, project=project, state_home=state, runner=_login_ok), 0)
        self.assertEqual(observe.command(second_out, home=home, project=project, state_home=state, runner=_login_ok), 0)
        first = json.loads(first_out.getvalue())
        second = json.loads(second_out.getvalue())
        first["sources"][0]["files"][0]["state"] = "changed-by-first"
        self.assertNotEqual(second["sources"][0]["files"][0]["state"], "changed-by-first")
        self.assertEqual(second["sources"][0]["files"][0]["allow"]["model"], "haiku-observe")

    def test_a_relative_state_home_and_an_unwritable_one_write_no_record(self):
        home, project, state = self._layout()
        _plant_denied(home)
        _watch_opens()
        before = len(_OPENED)
        out = io.StringIO()
        code = _observe().command(out, home=home, project=project, state_home="relative-state", runner=_login_ok)
        self.assertEqual(code, 1)
        answer = json.loads(out.getvalue())
        self.assertEqual(answer["code"], "state_home_unknown")
        _clean(out.getvalue())
        self.assertFalse(os.path.exists(os.path.join(project, "relative-state")))
        for raw in _OPENED[before:]:
            self.assertFalse(str(raw).endswith("auth.json"), raw)

        blocked = os.path.join(state, "not-a-directory")
        _write(blocked, "x", 0o600)
        out = io.StringIO()
        code = _observe().command(out, home=home, project=project, state_home=blocked, runner=_login_ok)
        self.assertEqual(code, 1)
        answer = json.loads(out.getvalue())
        self.assertEqual(answer["code"], "filesystem_error")
        _clean(out.getvalue())
        self.assertNotIn(_CANARY_AUTH, answer["detail"])

    def test_an_oversize_file_is_unknown_and_unread(self):
        home, project, state = self._layout()
        path = os.path.join(home, ".claude", "settings.json")
        _write(path, json.dumps({"model": "haiku-observe", "env": {"K": _CANARY_ENV}}), 0o600)
        observe = _observe()
        with mock.patch.object(observe, "_MAX_BYTES", 4):
            code, text, _record, disk, opened = self._run(home, project, state)
        self.assertEqual(code, 0, text)
        self._assert_private(home, opened, text, disk)
        row = _by_path(json.loads(text), path)
        self.assertEqual(row["state"], "unknown")
        self.assertIsNone(row["sha256"])
        shown = [os.fsdecode(raw) if isinstance(raw, bytes) else str(raw) for raw in opened]
        self.assertFalse(any(item.endswith("settings.json") for item in shown), shown)

    def test_observe_is_the_subcommand_row_and_refuses_off_linux(self):
        _observe()
        names = [row[0] for row in entry.SUBCOMMANDS]
        self.assertEqual(names.count("observe"), 1)
        self.assertEqual(names.count("doctor"), 1)
        self.assertEqual(len(names), len(set(names)))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = entry.main(["--help"])
        self.assertEqual(code, 0)
        help_text = out.getvalue()
        self.assertEqual(help_text.count("\n    observe "), 1)
        self.assertEqual(help_text.count("\n    doctor "), 1)

        with mock.patch.object(entry, "_linux", return_value=True):
            with contextlib.redirect_stdout(io.StringIO()) as out, \
                    contextlib.redirect_stderr(io.StringIO()):
                code = entry.main(["observe", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("--json", out.getvalue())

        home, project, state = self._layout()
        _plant_denied(home)
        with mock.patch.object(entry, "_linux", return_value=False), \
                mock.patch.dict(os.environ, {"HOME": home, "XDG_STATE_HOME": state, "PATH": ""}, clear=False):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = entry.main(["observe"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["code"], "platform_unsupported")
        self.assertFalse(os.path.exists(os.path.join(state, "recursion", "observe", "record.json")))

        with mock.patch.object(entry, "_linux", return_value=True), \
                mock.patch.dict(os.environ, {"HOME": home, "XDG_STATE_HOME": state, "PATH": ""}, clear=False), \
                contextlib.chdir(project):
            with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                code = entry.main(["observe"])
        self.assertEqual(code, 0, out.getvalue())
        report = json.loads(out.getvalue())
        self.assertEqual(report["code"], "ok")
        record = os.path.join(state, "recursion", "observe", "record.json")
        self.assertEqual(stat.S_IMODE(os.stat(record).st_mode), 0o600)
        _clean(out.getvalue())
        with open(record, encoding="ascii") as handle:
            _clean(handle.read())

        with mock.patch.object(entry, "_linux", return_value=True), \
                mock.patch.dict(os.environ, {"HOME": home, "XDG_STATE_HOME": state, "PATH": ""}, clear=False), \
                contextlib.chdir(project):
            with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                code = entry.main(["observe", "--json"])
        self.assertEqual(code, 0, out.getvalue())
        again = json.loads(out.getvalue())
        self.assertEqual(again["code"], "ok")
        _clean(out.getvalue())
        with open(record, encoding="ascii") as handle:
            _clean(handle.read())

    def _linked_payload(self, model):
        return json.dumps({
            "model": model,
            "env": {"TOKEN": _CANARY_LINK},
            "mcpServers": {
                "maps": {"command": f"/usr/lib/map-tool --token {_CANARY_CMD}"},
            },
        })

    def _assert_aliased_unread(self, home, project, state, path):
        code, text, _record, disk, opened = self._run(home, project, state)
        self.assertEqual(code, 0)
        _clean(text)
        _clean(disk.decode("utf-8", "replace"))
        self.assertFalse(_ends(opened, "settings.json"), "settings file was opened")
        self.assertFalse(_ends(opened, "cred.json"), "denied file was opened")
        row = _by_path(json.loads(text), path)
        self.assertEqual(row["state"], "aliased")
        self.assertIsNone(row["sha256"])
        self.assertNotIn("allow", row)
        self.assertIs("model-from-link" in text, False, "a model was copied from an alias")

    def test_a_hard_link_into_a_denied_directory_is_not_opened(self):
        # Witness observe-hardlink-denied-dir: OPENED.
        home, project, state = self._layout()
        cred = os.path.join(home, ".hermes", "cred.json")
        _write(cred, self._linked_payload("model-from-link"), 0o600)
        settings = os.path.join(home, ".claude", "settings.json")
        os.makedirs(os.path.dirname(settings), mode=0o700, exist_ok=True)
        os.link(cred, settings)
        self._assert_aliased_unread(home, project, state, settings)

    def test_a_hard_link_into_a_denied_directory_behind_a_symlink_is_not_opened(self):
        # Witness observe-hardlink-symlink-store: OPENED. The denied directory is a symlink.
        root = tempfile.TemporaryDirectory(prefix="observe-")
        self.addCleanup(root.cleanup)
        home = os.path.join(root.name, "home")
        project = os.path.join(root.name, "project")
        state = os.path.join(root.name, "state")
        store = os.path.join(root.name, "store")
        os.mkdir(home, 0o700)
        os.mkdir(project, 0o700)
        os.mkdir(state, 0o700)
        os.mkdir(store, 0o700)
        cred = os.path.join(store, "cred.json")
        _write(cred, self._linked_payload("model-from-link"), 0o600)
        os.symlink(store, os.path.join(home, ".hermes"))
        settings = os.path.join(home, ".claude", "settings.json")
        os.makedirs(os.path.dirname(settings), mode=0o700, exist_ok=True)
        os.link(cred, settings)
        self._assert_aliased_unread(home, project, state, settings)

    def test_any_other_hard_link_is_not_opened(self):
        home, project, state = self._layout()
        settings = os.path.join(home, ".claude", "settings.json")
        _write(settings, self._linked_payload("model-from-link"), 0o600)
        other = os.path.join(home, "notes", "copy.json")
        os.makedirs(os.path.dirname(other), mode=0o700, exist_ok=True)
        os.link(settings, other)
        self._assert_aliased_unread(home, project, state, settings)

    def test_a_descriptor_whose_inode_changes_is_not_hashed(self):
        # lstat said one link; fstat on the same descriptor must say the same inode.
        home, project, state = self._layout()
        settings = os.path.join(home, ".claude", "settings.json")
        _write(settings, json.dumps({"model": _CANARY_LINK}), 0o600)
        observe = _observe()
        real_fstat = os.fstat

        class _Shifted:
            def __init__(self, info):
                self._info = info

            def __getattr__(self, name):
                if name == "st_ino":
                    return self._info.st_ino + 1
                return getattr(self._info, name)

        def fstat(fd):
            info = real_fstat(fd)
            if stat.S_ISREG(info.st_mode) and info.st_size < 100000:
                return _Shifted(info)
            return info

        out = io.StringIO()
        with mock.patch("os.fstat", fstat):
            code = observe.command(out, home=home, project=project, state_home=state, runner=_login_ok)
        text = out.getvalue()
        self.assertEqual(code, 0)
        _clean(text)
        row = _by_path(json.loads(text), settings)
        self.assertEqual(row["state"], "aliased")
        self.assertIsNone(row["sha256"])
        self.assertNotIn("allow", row)

    def test_an_ancestor_symlink_outside_the_home_is_not_read(self):
        # Witness observe-parent-symlink: READ.
        root = tempfile.TemporaryDirectory(prefix="observe-")
        self.addCleanup(root.cleanup)
        home = os.path.join(root.name, "home")
        project = os.path.join(root.name, "project")
        state = os.path.join(root.name, "state")
        outside = os.path.join(root.name, "outside")
        os.mkdir(home, 0o700)
        os.mkdir(project, 0o700)
        os.mkdir(state, 0o700)
        os.mkdir(outside, 0o700)
        _write(
            os.path.join(outside, "settings.json"),
            self._linked_payload("model-from-outside"),
            0o600,
        )
        os.symlink(outside, os.path.join(home, ".claude"))
        code, text, _record, disk, opened = self._run(home, project, state)
        self.assertEqual(code, 0)
        _clean(text)
        _clean(disk.decode("utf-8", "replace"))
        row = _by_path(json.loads(text), os.path.join(home, ".claude", "settings.json"))
        self.assertNotEqual(row["state"], "ok")
        self.assertIsNone(row["sha256"])
        self.assertNotIn("allow", row)
        self.assertIs("model-from-outside" in text, False, "a model from outside the home was copied")
        self.assertFalse(_ends(opened, "settings.json"), "settings file was opened")

    def test_an_ancestor_symlink_outside_the_project_is_not_read(self):
        root = tempfile.TemporaryDirectory(prefix="observe-")
        self.addCleanup(root.cleanup)
        home = os.path.join(root.name, "home")
        project = os.path.join(root.name, "project")
        state = os.path.join(root.name, "state")
        outside = os.path.join(root.name, "outside")
        os.mkdir(home, 0o700)
        os.mkdir(project, 0o700)
        os.mkdir(state, 0o700)
        os.mkdir(outside, 0o700)
        _write(
            os.path.join(outside, "settings.json"),
            self._linked_payload("model-from-outside"),
            0o600,
        )
        os.symlink(outside, os.path.join(project, ".claude"))
        code, text, _record, disk, opened = self._run(home, project, state)
        self.assertEqual(code, 0)
        _clean(text)
        _clean(disk.decode("utf-8", "replace"))
        row = _by_path(json.loads(text), os.path.join(project, ".claude", "settings.json"))
        self.assertNotEqual(row["state"], "ok")
        self.assertIsNone(row["sha256"])
        self.assertNotIn("allow", row)
        self.assertIs("model-from-outside" in text, False, "a model from outside the project was copied")
        self.assertFalse(_ends(opened, "settings.json"), "settings file was opened")

    def test_an_ancestor_symlink_into_a_denied_directory_is_not_read(self):
        # The real path stays inside the home and lands under a denied directory.
        home, project, state = self._layout()
        store = os.path.join(home, "secretstore")
        os.mkdir(store, 0o700)
        _write(os.path.join(store, "settings.json"), self._linked_payload("model-from-denied"), 0o600)
        os.symlink(store, os.path.join(home, ".hermes"))
        os.symlink(store, os.path.join(home, ".claude"))
        code, text, _record, disk, opened = self._run(home, project, state)
        self.assertEqual(code, 0)
        _clean(text)
        _clean(disk.decode("utf-8", "replace"))
        row = _by_path(json.loads(text), os.path.join(home, ".claude", "settings.json"))
        self.assertNotEqual(row["state"], "ok")
        self.assertIsNone(row["sha256"])
        self.assertNotIn("allow", row)
        self.assertIs("model-from-denied" in text, False, "a model from a denied directory was copied")
        self.assertFalse(_ends(opened, "settings.json"), "settings file was opened")

    def test_an_ancestor_symlink_that_stays_inside_the_home_is_read(self):
        # Assumption: a real path that stays in the root and is not denied is read.
        home, project, state = self._layout()
        alt = os.path.join(home, "alt-claude")
        os.mkdir(alt, 0o700)
        settings_real = os.path.join(alt, "settings.json")
        body = json.dumps({"model": "haiku-inside"})
        _write(settings_real, body, 0o600)
        os.symlink(alt, os.path.join(home, ".claude"))
        code, text, _record, disk, opened = self._run(home, project, state)
        self.assertEqual(code, 0, text)
        _clean(text)
        _clean(disk.decode("utf-8", "replace"))
        row = _by_path(json.loads(text), os.path.join(home, ".claude", "settings.json"))
        self.assertEqual(row["state"], "ok")
        self.assertEqual(row["allow"]["model"], "haiku-inside")
        self.assertEqual(row["sha256"], hashlib.sha256(body.encode()).hexdigest())

    def test_a_home_without_local_or_xdg_state_home_gets_a_record(self):
        # Witness observe-fresh-home: NO-RECORD.
        home, project, _state = self._layout()
        self.assertFalse(os.path.exists(os.path.join(home, ".local")))
        out = io.StringIO()
        with _umask_open():
            code = _observe().command(
                out, home=home, project=project, state_home=None, runner=_login_ok, environ={},
            )
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        record = os.path.join(home, ".local", "state", "recursion", "observe", "record.json")
        self.assertTrue(os.path.isfile(record))
        self.assertFalse(os.path.islink(os.path.join(home, ".local")))
        self.assertEqual(stat.S_IMODE(os.lstat(os.path.join(home, ".local")).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.lstat(os.path.join(home, ".local", "state")).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.lstat(os.path.dirname(record)).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.lstat(record).st_mode), 0o600)
        report = json.loads(text)
        self.assertEqual(report["code"], "ok")
        self.assertEqual(report["record"], record)
        with open(record, encoding="ascii") as handle:
            self.assertEqual(handle.read(), text)

    def test_a_symlink_at_local_refuses_and_writes_nothing(self):
        root = tempfile.TemporaryDirectory(prefix="observe-")
        self.addCleanup(root.cleanup)
        home = os.path.join(root.name, "home")
        project = os.path.join(root.name, "project")
        outside = os.path.join(root.name, "outside")
        os.mkdir(home, 0o700)
        os.mkdir(project, 0o700)
        os.mkdir(outside, 0o700)
        os.symlink(outside, os.path.join(home, ".local"))
        out = io.StringIO()
        code = _observe().command(
            out, home=home, project=project, state_home=None, runner=_login_ok, environ={},
        )
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["code"], "filesystem_error")
        self.assertFalse(os.path.exists(os.path.join(outside, "state")))
        for _dirpath, _dirs, files in os.walk(outside):
            self.assertNotIn("record.json", files)

    def test_mkdir_tolerates_the_other_caller(self):
        # Witness observe-concurrent-mkdir: FAIL (FileExistsError beside ok).
        home, project, state = self._layout()
        observe_dir = os.path.join(state, "recursion", "observe")
        real_mkdir = os.mkdir

        def mkdir(path, mode=0o777, *, dir_fd=None):
            if dir_fd is None:
                real_mkdir(path, mode)
            else:
                real_mkdir(path, mode, dir_fd=dir_fd)
            if os.path.abspath(path) == observe_dir:
                raise FileExistsError(errno.EEXIST, "File exists", path)

        out = io.StringIO()
        with mock.patch("os.mkdir", mkdir):
            code = _observe().command(
                out, home=home, project=project, state_home=state, runner=_login_ok,
            )
        self.assertEqual(code, 0, "mkdir from the other caller was not tolerated")
        record = os.path.join(observe_dir, "record.json")
        self.assertTrue(os.path.isfile(record))
        self.assertEqual(stat.S_IMODE(os.lstat(record).st_mode), 0o600)
        self.assertEqual(json.loads(out.getvalue())["code"], "ok")

    def test_a_writer_does_not_share_one_temp_name(self):
        # Witness observe-shared-tmp-race: MISMATCH. The shared name is record.json.tmp.
        home, project, state = self._layout()
        opened = []
        real_open = os.open

        def open_hook(path, flags, mode=0o777, *, dir_fd=None):
            if isinstance(path, str) and "record.json" in os.path.basename(path):
                opened.append(os.path.basename(path))
            if dir_fd is None:
                return real_open(path, flags, mode)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with mock.patch("os.open", open_hook):
            code, text, record, disk, _opened = self._run(home, project, state)
        self.assertEqual(code, 0, text)
        temps = [name for name in opened if name != "record.json"]
        self.assertTrue(temps, "a record temp name was not opened")
        self.assertFalse(any(name == "record.json.tmp" for name in temps), "writers share one temp name")
        self.assertEqual(disk.decode("ascii"), text)

    def test_two_callers_at_once_each_print_a_whole_record(self):
        home, project, state = self._layout()
        _write(
            os.path.join(home, ".claude", "settings.json"),
            json.dumps({"model": "haiku-observe"}),
            0o600,
        )
        observe = _observe()
        results = []
        barrier = threading.Barrier(2)

        def call():
            out = io.StringIO()
            barrier.wait(timeout=5)
            code = observe.command(out, home=home, project=project, state_home=state, runner=_login_ok)
            results.append((code, out.getvalue()))

        threads = [threading.Thread(target=call), threading.Thread(target=call)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 2)
        texts = []
        for code, text in results:
            self.assertEqual(code, 0, text)
            report = json.loads(text)
            self.assertEqual(report["code"], "ok")
            self.assertIn("\n", text)
            texts.append(text)
        record = os.path.join(state, "recursion", "observe", "record.json")
        with open(record, encoding="ascii") as handle:
            final = handle.read()
        self.assertIn(final, texts)
        json.loads(final)

    def test_an_mcp_command_keeps_the_program_name_only(self):
        home, project, state = self._layout()
        settings = {
            "model": "haiku-observe",
            "mcpServers": {
                "maps": {"command": f"/usr/lib/map-tool --token {_CANARY_CMD} extra"},
            },
        }
        path = os.path.join(home, ".claude", "settings.json")
        _write(path, json.dumps(settings), 0o600)
        code, text, _record, disk, _opened = self._run(home, project, state)
        self.assertEqual(code, 0)
        _clean(text)
        _clean(disk.decode("utf-8", "replace"))
        row = _by_path(json.loads(text), path)
        servers = row["allow"]["mcp_servers"]
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]["name"], "maps")
        self.assertIs(servers[0]["command"] == "map-tool", True, "mcp command is not the program name")

    def test_a_git_section_header_may_end_with_a_comment(self):
        # Witness observe-git-parser: DIFF. A trailing comment still names the section.
        home, project, state = self._layout()
        include = os.path.join(home, "included-git-config")
        _write(include, f"[user]\n    name = {_CANARY_GIT_URL}\n", 0o644)
        gitconfig = (
            "[user] # trailing comment\n"
            f"    name = {_CANARY_GIT_NAME}\n"
            f"    email = {_CANARY_GIT_MAIL}\n"
            "[include]\n"
            f"    path = {include}\n"
        )
        git_path = os.path.join(home, ".gitconfig")
        _write(git_path, gitconfig, 0o644)
        xdg = (
            "[user] ; semicolon comment\n"
            f"    name = {_CANARY_GIT_NAME}\n"
            f"    email = {_CANARY_GIT_MAIL}\n"
        )
        xdg_path = os.path.join(home, ".config", "git", "config")
        _write(xdg_path, xdg, 0o644)
        code, text, _record, disk, opened = self._run(home, project, state)
        self.assertEqual(code, 0)
        _clean(text)
        _clean(disk.decode("utf-8", "replace"))
        self.assertFalse(any(include == name or name.endswith("included-git-config") for name in _opened_names(opened)))
        git_row = _by_path(json.loads(text), git_path)
        self.assertEqual(git_row["state"], "ok")
        self.assertIs(git_row["allow"]["user_name_present"], True)
        self.assertIs(git_row["allow"]["user_email_present"], True)
        xdg_row = _by_path(json.loads(text), xdg_path)
        self.assertIs(xdg_row["allow"]["user_name_present"], True)
        self.assertIs(xdg_row["allow"]["user_email_present"], True)


if __name__ == "__main__":
    unittest.main()
