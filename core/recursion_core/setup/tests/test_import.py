"""``recursiond import --from codex|claude|cursor --dry-run`` proposes a config and writes nothing.

Red on the observe tip: ``import`` is not a command, so the dry-run exits with a usage error
and prints no proposal. Once the command exists, the same test is red if a canary planted in
``.codex/auth.json``, ``.claude/.credentials.json``, a settings ``env`` value, or an MCP
``headers`` value reaches stdout, stderr, or any file under the fake profile, if the profile
tree changes, or if folder trust is proposed as trusted.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

from recursion_core import _entrypoints as entry

_CANARY_AUTH = "CANARY-AUTH-import-7c1e9a4b-DO-NOT-LEAK"
_CANARY_CRED = "CANARY-CRED-import-7c1e9a4b-DO-NOT-LEAK"
_CANARY_ENV = "CANARY-ENV-import-7c1e9a4b-DO-NOT-LEAK"
_CANARY_HDR = "CANARY-HDR-import-7c1e9a4b-DO-NOT-LEAK"
_CANARY_CMD = "CANARY-CMD-import-7c1e9a4b-DO-NOT-LEAK"
_CANARY_HOOK = "CANARY-HOOK-import-7c1e9a4b-DO-NOT-LEAK"
_CANARY_SEALED = "CANARY-SEALED-import-7c1e9a4b-DO-NOT-LEAK"
_CANARIES = (
    _CANARY_AUTH, _CANARY_CRED, _CANARY_ENV, _CANARY_HDR, _CANARY_CMD, _CANARY_HOOK, _CANARY_SEALED,
)
_OPENED: list = []
_HOOKED = False


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


def _proposal_module():
    import importlib

    return importlib.import_module("recursion_core.setup.import_")


def _write(path, text, mode=0o600):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, mode)


def _plant_denied(home):
    import recursion_core.setup.deny as deny

    blob = _CANARY_AUTH + "\n" + _CANARY_CRED + "\n"
    for path in deny.denied_paths(home):
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        leaf = os.path.basename(path)
        if leaf.startswith(".") or path.endswith(".json"):
            _write(path, blob, 0o600)
        else:
            os.makedirs(path, exist_ok=True)
            _write(os.path.join(path, "secret.txt"), blob, 0o600)


def _plant_profile(home):
    """Canaries in the stores and settings the order names, plus a trusted-folder mark."""

    _plant_denied(home)
    _write(os.path.join(home, ".codex", "auth.json"), json.dumps({"access_token": _CANARY_AUTH}), 0o600)
    _write(
        os.path.join(home, ".claude", ".credentials.json"),
        json.dumps({"claudeAiOauth": _CANARY_CRED}),
        0o600,
    )
    _write(os.path.join(home, ".claude.json"), json.dumps({
        "oauth": _CANARY_SEALED,
        "projects": {"/work/claude-sealed": {"hasTrustDialogAccepted": True}},
    }), 0o600)
    settings = {
        "model": "sonnet-import",
        "env": {"ANTHROPIC_API_KEY": _CANARY_ENV},
        "hooks": {"PreToolUse": [{"command": _CANARY_HOOK}]},
        "projects": {"/work/claude-app": {"trust": "trusted", "hasTrustDialogAccepted": True}},
        "mcpServers": {
            "maps": {
                "command": _CANARY_CMD,
                "args": ["--flag", _CANARY_ENV],
                "env": {"TOKEN": _CANARY_ENV},
                "headers": {"Authorization": _CANARY_HDR},
            }
        },
    }
    _write(os.path.join(home, ".claude", "settings.json"), json.dumps(settings), 0o600)
    toml = (
        'model = "gpt-import"\n'
        'approval_policy = "on-request"\n'
        "\n"
        '[projects."/work/codex-app"]\n'
        'trust_level = "trusted"\n'
        "\n"
        "[mcp_servers.maps]\n"
        f'command = "{_CANARY_CMD}"\n'
        f'args = ["--flag", "{_CANARY_ENV}"]\n'
        "\n"
        "[mcp_servers.maps.env]\n"
        f'TOKEN = "{_CANARY_ENV}"\n'
        "\n"
        "[mcp_servers.maps.http_headers]\n"
        f'Authorization = "{_CANARY_HDR}"\n'
    )
    _write(os.path.join(home, ".codex", "config.toml"), toml, 0o600)
    cursor = {
        "mcpServers": {
            "draw": {
                "command": _CANARY_CMD,
                "env": {"TOKEN": _CANARY_ENV},
                "headers": {"Authorization": _CANARY_HDR},
            }
        }
    }
    _write(os.path.join(home, ".cursor", "mcp.json"), json.dumps(cursor), 0o600)


def _tree_hash(root: str) -> str:
    rows = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        rel_dir = os.path.relpath(dirpath, root)
        rows.append(("dir", rel_dir, stat.S_IMODE(os.lstat(dirpath).st_mode)))
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            info = os.lstat(path)
            rel = os.path.relpath(path, root)
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                rows.append(("link", rel, os.readlink(path), mode))
            elif stat.S_ISREG(info.st_mode):
                try:
                    digest = hashlib.sha256()
                    with open(path, "rb") as handle:
                        digest.update(handle.read())
                    rows.append(("file", rel, mode, info.st_size, digest.hexdigest()))
                except PermissionError:
                    rows.append(("unreadable", rel, mode, info.st_size))
            else:
                rows.append(("other", rel, mode, info.st_size))
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


def _clean(blob: str) -> None:
    for canary in _CANARIES:
        if canary in blob:
            raise AssertionError(f"canary reached the output: {canary}")


def _walk_strings(node, found: list) -> None:
    if type(node) is str:
        found.append(node)
    elif type(node) is dict:
        for key, value in node.items():
            _walk_strings(key, found)
            _walk_strings(value, found)
    elif type(node) is list:
        for item in node:
            _walk_strings(item, found)


def _under_home(opened, home: str) -> list:
    rows = []
    prefix = home if home.endswith(os.sep) else home + os.sep
    for raw in opened:
        shown = os.fsdecode(raw) if isinstance(raw, (bytes, str)) else str(raw)
        if isinstance(raw, bytes):
            try:
                shown = os.fsdecode(raw)
            except Exception:
                shown = str(raw)
        if shown == home or shown.startswith(prefix):
            rows.append(shown)
    return rows


class ImportDryRunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="import-propose-")
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.home = os.path.join(self.root, "home")
        self.state = os.path.join(self.root, "state")
        self.config = os.path.join(self.root, "config")
        os.mkdir(self.home, 0o700)
        os.mkdir(self.state, 0o700)
        os.mkdir(self.config, 0o700)
        _plant_profile(self.home)

    def _environ(self):
        return {
            "HOME": self.home,
            "XDG_STATE_HOME": self.state,
            "XDG_CONFIG_HOME": self.config,
            "XDG_DATA_HOME": os.path.join(self.root, "data"),
        }

    def _cli(self, argv):
        _watch_opens()
        before = len(_OPENED)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self._environ(), clear=False), \
                mock.patch("subprocess.run", side_effect=AssertionError("import starts no child")), \
                mock.patch("os.execv", side_effect=AssertionError("import starts no child")):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = entry.main(list(argv))
        return code, out.getvalue(), err.getvalue(), _OPENED[before:]

    def _assert_silent(self, before_hash, out, err, opened):
        _clean(out)
        _clean(err)
        self.assertEqual(_tree_hash(self.root), before_hash)
        import recursion_core.setup.deny as deny

        for raw in opened:
            shown = os.fsdecode(raw) if isinstance(raw, bytes) else str(raw)
            self.assertFalse(deny.is_denied(shown, self.home), shown)
            self.assertFalse(shown.endswith("auth.json"), shown)
            self.assertFalse(shown.endswith(".credentials.json"), shown)
            self.assertFalse(shown.endswith(".claude.json"), shown)
        for dirpath, _dirs, filenames in os.walk(self.root):
            for name in filenames:
                path = os.path.join(dirpath, name)
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
                with open(path, "rb") as handle:
                    blob = handle.read()
                # Planted canaries stay in the files the test wrote. A new file must not hold one.
                rel = os.path.relpath(path, self.home)
                if rel.startswith(".." + os.sep) or rel == "..":
                    text = blob.decode("utf-8", "replace")
                    for canary in _CANARIES:
                        self.assertNotIn(canary, text, path)

    def test_dry_run_prints_a_proposal_and_writes_nothing(self):
        before = _tree_hash(self.root)
        expected = {
            "codex": ("codex", "gpt-import", "/work/codex-app", os.path.join(self.home, ".codex", "config.toml")),
            "claude": ("claude_code", "sonnet-import", "/work/claude-app",
                       os.path.join(self.home, ".claude", "settings.json")),
            "cursor": (None, None, None, os.path.join(self.home, ".cursor", "mcp.json")),
        }
        for source, (engine, model, folder, settings_path) in expected.items():
            with self.subTest(source=source):
                code, out, err, opened = self._cli(["import", "--from", source, "--dry-run"])
                self.assertEqual(err, "", err)
                self.assertEqual(code, 0, (out, err))
                answer = json.loads(out)
                self.assertEqual(set(answer), {"action", "code", "dry_run", "from", "proposal", "wrote"})
                self.assertEqual(answer["action"], "import")
                self.assertEqual(answer["code"], "ok")
                self.assertIs(answer["dry_run"], True)
                self.assertIs(answer["wrote"], False)
                self.assertEqual(answer["from"], source)
                proposal = answer["proposal"]
                self.assertEqual(proposal["default_engine"], engine)
                self.assertEqual(proposal["folder_trust"], "user_choice")
                strings = []
                _walk_strings(answer, strings)
                self.assertNotIn("trusted", strings)
                for canary in _CANARIES:
                    self.assertNotIn(canary, strings)
                self.assertNotIn(_CANARY_CMD, out)
                if model is None:
                    self.assertNotIn("model", proposal)
                else:
                    self.assertEqual(proposal["model"], model)
                if folder is None:
                    self.assertNotIn("folders", proposal)
                else:
                    self.assertEqual(proposal["folders"], [{"path": folder, "trust": "user_choice"}])
                home_opens = _under_home(opened, self.home)
                self.assertEqual(home_opens, [settings_path], home_opens)
                self._assert_silent(before, out, err, opened)
                self.assertFalse(os.path.exists(os.path.join(self.config, "recursion", "routing.json")))
                self.assertFalse(os.path.exists(os.path.join(self.state, "recursion")))

    def test_a_second_call_still_writes_nothing(self):
        module = _proposal_module()
        before = _tree_hash(self.root)
        out_a, out_b = io.StringIO(), io.StringIO()
        code_a = module.command(out_a, source="codex", dry_run=True, home=self.home, environ=self._environ())
        code_b = module.command(out_b, source="codex", dry_run=True, home=self.home, environ=self._environ())
        self.assertEqual((code_a, code_b), (0, 0))
        self.assertEqual(out_a.getvalue(), out_b.getvalue())
        _clean(out_a.getvalue())
        self.assertEqual(_tree_hash(self.root), before)
        first = json.loads(out_a.getvalue())
        self.assertEqual(first["proposal"]["default_engine"], "codex")
        self.assertEqual(first["proposal"]["folder_trust"], "user_choice")

    def test_without_dry_run_and_with_apply_write_nothing(self):
        before = _tree_hash(self.root)
        code, out, err, opened = self._cli(["import", "--from", "codex"])
        self.assertEqual(code, 1, (out, err))
        answer = json.loads(out)
        self.assertEqual(answer["code"], "dry_run_required")
        self._assert_silent(before, out, err, opened)
        self.assertEqual(_under_home(opened, self.home), [])

        code, out, err, opened = self._cli(["import", "--from", "codex", "--apply"])
        self.assertEqual(code, 2, (out, err))
        self.assertEqual(out, "")
        self.assertIn("usage error", err)
        self._assert_silent(before, out, err, opened)
        self.assertFalse(os.path.exists(os.path.join(self.config, "recursion", "routing.json")))

    def test_a_bad_source_or_a_relative_home_writes_nothing(self):
        before = _tree_hash(self.root)
        code, out, err, opened = self._cli(["import", "--from", "git", "--dry-run"])
        self.assertEqual(code, 2, (out, err))
        self.assertEqual(out, "")
        self._assert_silent(before, out, err, opened)

        module = _proposal_module()
        out = io.StringIO()
        code = module.command(out, source="codex", dry_run=True, home="relative/home", environ={})
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["code"], "home_unknown")
        _clean(out.getvalue())
        self.assertEqual(_tree_hash(self.root), before)

        out = io.StringIO()
        code = module.command(out, source="codex", dry_run=True, home="", environ={})
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["code"], "home_unknown")
        self.assertEqual(_tree_hash(self.root), before)

    def test_a_raised_read_does_not_carry_the_canary(self):
        module = _proposal_module()
        before = _tree_hash(self.root)

        def boom(path, home, denied_ids):
            raise OSError(_CANARY_ENV)

        with mock.patch.object(module, "_read_bytes", boom):
            out = io.StringIO()
            code = module.command(out, source="claude", dry_run=True, home=self.home, environ=self._environ())
        self.assertEqual(code, 0, out.getvalue())
        answer = json.loads(out.getvalue())
        self.assertEqual(answer["proposal"]["default_engine"], "claude_code")
        self.assertEqual(answer["proposal"]["folder_trust"], "user_choice")
        self.assertNotIn("model", answer["proposal"])
        _clean(out.getvalue())
        self.assertEqual(_tree_hash(self.root), before)

    def test_interruption_writes_nothing_and_propagates(self):
        module = _proposal_module()
        before = _tree_hash(self.root)

        def interrupt(path, home, denied_ids):
            raise KeyboardInterrupt

        out = io.StringIO()
        with mock.patch.object(module, "_read_bytes", interrupt):
            with self.assertRaises(KeyboardInterrupt):
                module.command(out, source="codex", dry_run=True, home=self.home, environ=self._environ())
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(_tree_hash(self.root), before)

    def test_a_symlink_or_hard_link_to_a_sign_in_store_is_not_opened(self):
        module = _proposal_module()
        auth = os.path.join(self.home, ".codex", "auth.json")
        config = os.path.join(self.home, ".codex", "config.toml")
        os.unlink(config)
        os.symlink(auth, config)
        before = _tree_hash(self.root)
        _watch_opens()
        mark = len(_OPENED)
        out = io.StringIO()
        code = module.command(out, source="codex", dry_run=True, home=self.home, environ=self._environ())
        self.assertEqual(code, 0, out.getvalue())
        answer = json.loads(out.getvalue())
        self.assertEqual(answer["proposal"]["default_engine"], "codex")
        self.assertNotIn("model", answer["proposal"])
        _clean(out.getvalue())
        opened = _under_home(_OPENED[mark:], self.home)
        self.assertEqual(opened, [], opened)
        self.assertEqual(_tree_hash(self.root), before)

        os.unlink(config)
        os.link(auth, config)
        before = _tree_hash(self.root)
        mark = len(_OPENED)
        out = io.StringIO()
        code = module.command(out, source="codex", dry_run=True, home=self.home, environ=self._environ())
        self.assertEqual(code, 0, out.getvalue())
        _clean(out.getvalue())
        opened = _under_home(_OPENED[mark:], self.home)
        self.assertFalse(any(path.endswith("config.toml") or path.endswith("auth.json") for path in opened), opened)
        self.assertEqual(_tree_hash(self.root), before)

    def test_a_mode_000_settings_file_is_not_opened(self):
        module = _proposal_module()
        path = os.path.join(self.home, ".codex", "config.toml")
        os.chmod(path, 0o000)
        self.addCleanup(os.chmod, path, 0o600)
        if os.geteuid() == 0:
            self.skipTest("root reads through mode 000")
        before = _tree_hash(self.root)
        _watch_opens()
        mark = len(_OPENED)
        out = io.StringIO()
        code = module.command(out, source="codex", dry_run=True, home=self.home, environ=self._environ())
        self.assertEqual(code, 0, out.getvalue())
        answer = json.loads(out.getvalue())
        self.assertEqual(answer["proposal"]["folder_trust"], "user_choice")
        self.assertNotIn("model", answer["proposal"])
        _clean(out.getvalue())
        opened = _under_home(_OPENED[mark:], self.home)
        self.assertFalse(any(item.endswith("config.toml") for item in opened), opened)
        self.assertEqual(_tree_hash(self.root), before)

    def test_import_refuses_off_linux_before_it_reads(self):
        before = _tree_hash(self.root)
        _watch_opens()
        mark = len(_OPENED)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(entry, "_linux", return_value=False), \
                mock.patch.dict(os.environ, self._environ(), clear=False):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = entry.main(["import", "--from", "codex", "--dry-run"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["code"], "platform_unsupported")
        self.assertEqual(_under_home(_OPENED[mark:], self.home), [])
        self.assertEqual(_tree_hash(self.root), before)

    def test_help_lists_import(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = entry.main(["--help"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("import", out.getvalue())
        with mock.patch.object(entry, "_linux", return_value=True):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = entry.main(["import", "--help"])
        self.assertEqual(code, 0, err.getvalue())
        text = out.getvalue()
        self.assertIn("--from", text)
        self.assertIn("--dry-run", text)


if __name__ == "__main__":
    unittest.main()
