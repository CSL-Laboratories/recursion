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
    # The failure text stays plain: it must not repeat a planted marker.
    for canary in _CANARIES:
        if canary in blob:
            raise AssertionError("canary reached the output")


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


def _off_posix() -> bool:
    # The simulated-Windows step takes O_NOFOLLOW away (and says win32): the gate and the entry point refuse.
    return not hasattr(os, "O_NOFOLLOW")


def _leaves(opened) -> list:
    """The names a run opened, as text. The gate opens the file by its leaf name from its parent."""

    rows = []
    for raw in opened:
        if isinstance(raw, bytes):
            raw = os.fsdecode(raw)
        if isinstance(raw, str):
            rows.append(os.path.basename(raw))
    return rows


def _key_shape(prefix: str) -> str:
    # Built at run time from a prefix and the word FAKE: obviously fake, never a real key.
    return prefix + "FAKE"


def _random_run(length: int) -> str:
    # A run that changes character class every character or two, the shape of a hex key.
    return "".join("0123456789abcdef"[(index * 7 + 3) % 16] for index in range(length))


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
        if _off_posix():
            # Simulated Windows: the entry point refuses before any read.
            for source in ("codex", "claude", "cursor"):
                with self.subTest(source=source):
                    self._assert_cli_refused(["import", "--from", source, "--dry-run"])
            return
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
                # The shared gate (merged from #533) walks to the file by directory descriptors and
                # opens it by its leaf name: no absolute path under the home, the leaf once.
                self.assertEqual(home_opens, [], home_opens)
                self.assertEqual(_leaves(opened).count(os.path.basename(settings_path)), 1)
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
        if _off_posix():
            self._assert_cli_refused(["import", "--from", "codex"])
            self._assert_cli_refused(["import", "--from", "codex", "--apply"])
            return
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
        if _off_posix():
            self._assert_cli_refused(["import", "--from", "git", "--dry-run"])
            return
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
        if _off_posix():
            self._assert_gate_refused(os.path.join(self.home, ".codex", "config.toml"))
            return
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

    def _claude(self):
        module = _proposal_module()
        _watch_opens()
        mark = len(_OPENED)
        out = io.StringIO()
        code = module.command(out, source="claude", dry_run=True, home=self.home, environ=self._environ())
        return code, out.getvalue(), _OPENED[mark:]

    def _replace_settings(self, payload: dict) -> str:
        path = os.path.join(self.home, ".claude", "settings.json")
        _write(path, json.dumps(payload), 0o600)
        return path

    def _assert_model_dropped(self, text: str) -> None:
        _clean(text)
        answer = json.loads(text)
        self.assertIs("model" in answer["proposal"], False, "model was not dropped")
        self.assertEqual(answer["proposal"]["default_engine"], "claude_code")
        self.assertEqual(answer["proposal"]["folder_trust"], "user_choice")

    def test_a_hard_link_into_a_denied_directory_is_not_opened(self):
        # Witness import-hardlink-denied-dir: OPENED.
        hermes = os.path.join(self.home, ".hermes")
        os.remove(hermes)
        os.mkdir(hermes, 0o700)
        cred = os.path.join(hermes, "cred.json")
        _write(cred, json.dumps({
            "model": "model-from-link",
            "env": {"TOKEN": _CANARY_ENV},
        }), 0o600)
        settings = os.path.join(self.home, ".claude", "settings.json")
        os.unlink(settings)
        os.link(cred, settings)
        before = _tree_hash(self.root)
        code, text, opened = self._claude()
        self.assertEqual(code, 0)
        self._assert_model_dropped(text)
        self.assertIs("model-from-link" in text, False, "a model was copied from an alias")
        shown = _under_home(opened, self.home)
        self.assertFalse(any(path.endswith("settings.json") for path in shown), "settings file was opened")
        self.assertFalse(any(path.endswith("cred.json") for path in shown), "denied file was opened")
        self.assertEqual(_tree_hash(self.root), before)

    def test_a_hard_link_into_a_denied_directory_behind_a_symlink_is_not_opened(self):
        # Witness import-hardlink-denied-dir with the denied directory itself a symlink.
        store = os.path.join(self.root, "store")
        os.mkdir(store, 0o700)
        cred = os.path.join(store, "cred.json")
        _write(cred, json.dumps({
            "model": "model-from-link",
            "env": {"TOKEN": _CANARY_ENV},
        }), 0o600)
        hermes = os.path.join(self.home, ".hermes")
        os.remove(hermes)
        os.symlink(store, hermes)
        settings = os.path.join(self.home, ".claude", "settings.json")
        os.unlink(settings)
        os.link(cred, settings)
        before = _tree_hash(self.root)
        code, text, opened = self._claude()
        self.assertEqual(code, 0)
        self._assert_model_dropped(text)
        self.assertIs("model-from-link" in text, False, "a model was copied from an alias")
        shown = _under_home(opened, self.home)
        self.assertFalse(any(path.endswith("settings.json") for path in shown), "settings file was opened")
        self.assertEqual(_tree_hash(self.root), before)

    def test_any_other_hard_link_is_not_opened(self):
        settings = self._replace_settings({
            "model": "model-from-link",
            "env": {"TOKEN": _CANARY_ENV},
        })
        other = os.path.join(self.home, "notes", "copy.json")
        os.makedirs(os.path.dirname(other), mode=0o700, exist_ok=True)
        os.link(settings, other)
        before = _tree_hash(self.root)
        code, text, opened = self._claude()
        self.assertEqual(code, 0)
        self._assert_model_dropped(text)
        self.assertIs("model-from-link" in text, False, "a model was copied from an alias")
        shown = _under_home(opened, self.home)
        self.assertFalse(any(path.endswith("settings.json") for path in shown), "settings file was opened")
        self.assertEqual(_tree_hash(self.root), before)

    def test_a_descriptor_whose_inode_changes_is_not_read(self):
        self._replace_settings({"model": "haiku-import", "env": {"TOKEN": _CANARY_ENV}})
        module = _proposal_module()
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
            code = module.command(out, source="claude", dry_run=True, home=self.home, environ=self._environ())
        text = out.getvalue()
        self.assertEqual(code, 0)
        self._assert_model_dropped(text)

    def test_a_short_secret_inside_model_is_dropped(self):
        # Witness import-short-secret-in-model: LEAK. Ends DROPPED.
        self._replace_settings({"model": "pre-SEKRET99-post", "api_key": "SEKRET99"})
        code, text, _opened = self._claude()
        self.assertEqual(code, 0)
        self._assert_model_dropped(text)
        self.assertIs("SEKRET99" in text, False, "a short value was copied")

    def test_a_command_equal_to_model_is_dropped(self):
        # Witness import-command-equals-model: LEAK. Ends DROPPED.
        self._replace_settings({
            "model": "map-tool",
            "mcpServers": {"maps": {"command": "map-tool"}},
        })
        code, text, _opened = self._claude()
        self.assertEqual(code, 0)
        self._assert_model_dropped(text)
        self.assertIs("map-tool" in text, False, "a command was copied as the model")

    def test_an_unlisted_key_equal_to_model_is_dropped(self):
        # Witness import-unlisted-key-equals-model: LEAK. Ends DROPPED.
        self._replace_settings({"model": "gpt-4o", "OPENAI_API_KEY": "gpt-4o"})
        code, text, _opened = self._claude()
        self.assertEqual(code, 0)
        self._assert_model_dropped(text)
        self.assertIs("gpt-4o" in text, False, "an unlisted value was copied as the model")

    def test_a_model_outside_the_name_shape_is_dropped(self):
        self._replace_settings({"model": "name!extra"})
        code, text, _opened = self._claude()
        self.assertEqual(code, 0)
        self._assert_model_dropped(text)

    def test_a_value_shorter_than_four_characters_does_not_drop_the_model(self):
        if _off_posix():
            self._assert_gate_refused(os.path.join(self.home, ".claude", "settings.json"))
            return
        self._replace_settings({"model": "abc-tool", "note": "abc"})
        code, text, _opened = self._claude()
        self.assertEqual(code, 0, text)
        _clean(text)
        answer = json.loads(text)
        self.assertEqual(answer["proposal"]["model"], "abc-tool")

    def test_a_four_character_value_inside_model_is_dropped(self):
        self._replace_settings({"model": "abcd-tool", "note": "abcd"})
        code, text, _opened = self._claude()
        self.assertEqual(code, 0)
        self._assert_model_dropped(text)

    # The answer round on the read a4b: the model and the folder paths pass the shared value
    # rule (setup/value_rule.py), and the merged gate refuses a foreign-owned or writable file.

    def _assert_cli_refused(self, argv) -> None:
        before = _tree_hash(self.root)
        code, out, err, opened = self._cli(argv)
        self.assertEqual(code, 1, err)
        self.assertEqual(json.loads(out)["code"], "platform_unsupported")
        self.assertEqual(_under_home(opened, self.home), [])
        self.assertEqual(_tree_hash(self.root), before)

    def _assert_gate_refused(self, path) -> None:
        from recursion_core.setup import open_guard

        with self.assertRaises(open_guard.GateRefused) as caught:
            open_guard.read_regular_file(path, root=self.home, home=self.home, max_bytes=1 << 20)
        self.assertEqual(caught.exception.reason, "platform_unsupported")

    def _proposal(self, source):
        module = _proposal_module()
        _watch_opens()
        mark = len(_OPENED)
        out = io.StringIO()
        code = module.command(out, source=source, dry_run=True, home=self.home, environ=self._environ())
        self.assertEqual(code, 0)
        text = out.getvalue()
        _clean(text)
        return json.loads(text)["proposal"], text, _OPENED[mark:]

    def test_a_model_with_a_credential_prefix_is_dropped(self):
        from recursion_core.setup import value_rule

        for prefix in value_rule.CREDENTIAL_PREFIXES:
            marker = _key_shape(prefix) + "model"
            with self.subTest(prefix=prefix):
                self._replace_settings({"model": marker})
                code, text, _opened = self._claude()
                self.assertEqual(code, 0)
                self._assert_model_dropped(text)
                self.assertIs(marker in text, False, "a key-shaped model was copied")

    def test_a_model_holding_a_random_run_is_dropped(self):
        marker = _key_shape("") + "-" + _random_run(24)
        self._replace_settings({"model": marker})
        code, text, _opened = self._claude()
        self.assertEqual(code, 0)
        self._assert_model_dropped(text)
        self.assertIs(marker in text, False, "a random-run model was copied")

    def test_a_model_with_a_key_inside_a_url_path_is_dropped(self):
        marker = "https://models.example/v1/" + _key_shape("sk-") + "keyinsidetheurlpath"
        self._replace_settings({"model": marker})
        code, text, _opened = self._claude()
        self.assertEqual(code, 0)
        self._assert_model_dropped(text)
        self.assertIs(marker in text, False, "a key inside a model path was copied")

    def test_harmless_models_are_still_shown(self):
        path = os.path.join(self.home, ".claude", "settings.json")
        if _off_posix():
            self._assert_gate_refused(path)
            return
        for model in ("gpt-5-codex", "claude-sonnet-4-5-20250929", "o3-mini",
                      "anthropic/claude-3.5-sonnet:beta", "llama3.1:8b"):
            with self.subTest(model=model):
                self._replace_settings({"model": model})
                proposal, _text, _opened = self._proposal("claude")
                self.assertEqual(proposal.get("model"), model)

    def test_a_codex_profile_that_repeats_the_model_still_shows_it(self):
        path = os.path.join(self.home, ".codex", "config.toml")
        _write(path, 'model = "gpt-5-codex"\n\n[profiles.fast]\nmodel = "gpt-5-codex"\n', 0o600)
        if _off_posix():
            self._assert_gate_refused(path)
            return
        proposal, _text, _opened = self._proposal("codex")
        self.assertEqual(proposal.get("model"), "gpt-5-codex")

    def test_a_key_shaped_folder_path_is_dropped(self):
        prefixed = "/work/" + _key_shape("ghp_") + "0123456789abcdef"
        random_path = "/work/" + _random_run(24)
        plain = "/work/plain-app"
        path = self._replace_settings({"projects": {prefixed: {}, random_path: {}, plain: {}}})
        if _off_posix():
            self._assert_gate_refused(path)
            return
        proposal, text, _opened = self._proposal("claude")
        folders = [row["path"] for row in proposal.get("folders", [])]
        self.assertIs(prefixed in text, False, "a prefixed folder path was copied")
        self.assertIs(random_path in text, False, "a random-run folder path was copied")
        self.assertIs(folders == [plain], True, "the plain folder path is not the one kept")

    def _assert_proposes_nothing(self, source, path) -> None:
        proposal, _text, opened = self._proposal(source)
        self.assertIs("model" in proposal, False, "a model was copied")
        self.assertIs("folders" in proposal, False, "a folder was copied")
        self.assertNotIn(os.path.basename(path), _leaves(opened))

    def test_a_foreign_owned_settings_file_proposes_nothing(self):
        path = os.path.join(self.home, ".codex", "config.toml")
        if _off_posix():
            self._assert_gate_refused(path)
            return
        uid = os.geteuid()
        with mock.patch("os.geteuid", return_value=uid + 1):
            self._assert_proposes_nothing("codex", path)

    def test_a_group_or_world_writable_settings_file_proposes_nothing(self):
        path = os.path.join(self.home, ".codex", "config.toml")
        self.addCleanup(os.chmod, path, 0o600)
        if _off_posix():
            self._assert_gate_refused(path)
            return
        for mode in (0o620, 0o602, 0o660, 0o666):
            with self.subTest(mode=oct(mode)):
                os.chmod(path, mode)
                self._assert_proposes_nothing("codex", path)


if __name__ == "__main__":
    unittest.main()
