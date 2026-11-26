"""Install by id from a registry named by its address and a full commit.

Every registry here is made up by the test in the marketplace layout and committed into a local bare
repository inside the test's own temporary folder. No test passes a network address to git: an
``https://`` address appears only where it is refused before git starts. Each refusal compares a
listing of the whole profile (the config and the state folder) before and after, and finds no scratch
folder left behind.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import unittest

from recursion_core.plugin_security.tests.test_plugin_cli_install import LINUX, LINUX_ONLY, ProfileCase

_PLUGIN = "fixture.unsigned"


def _git(args, cwd=None):
    run = subprocess.run(["git", *args], check=True, capture_output=True, text=True, cwd=cwd,
                         env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, timeout=60)
    return run.stdout


class RegistryCase(ProfileCase):
    def setUp(self):
        super().setUp()
        # git and the fetch's scratch folder stay inside this test's own folder.
        scratch = self.tmp / "tmp"
        scratch.mkdir(mode=0o700)
        home = self.tmp / "home"
        home.mkdir(mode=0o700)
        saved = {name: os.environ.get(name) for name in ("TMPDIR", "HOME", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM")}

        def restore():
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.addCleanup(restore)
        os.environ.update({"TMPDIR": str(scratch), "HOME": str(home),
                           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull})
        self.scratch = scratch

    def commit_registry(self, *, extra=None, links=None, gitlink=False, tag=None):
        """Commit the registry folder (plus ``extra`` files and ``links``) and return (bare, commit)."""
        work = self.registry
        for name, data in (extra or {}).items():
            path = work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        for name, target in (links or {}).items():
            path = work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, path)
        _git(["init", "-q", "-b", "main", str(work)])
        _git(["-C", str(work), "add", "-A"])
        if gitlink:
            _git(["-C", str(work), "update-index", "--add", "--cacheinfo",
                  "160000,1111111111111111111111111111111111111111,registry/sub"])
        _git(["-C", str(work), "-c", "user.name=registry-fixture", "-c", "user.email=registry-fixture@example.invalid",
              "commit", "-q", "-m", "registry"])
        if tag:
            _git(["-C", str(work), "tag", tag])
        commit = _git(["-C", str(work), "rev-parse", "HEAD"]).strip()
        bare = self.tmp / "registry.git"
        _git(["clone", "--bare", "-q", str(work), str(bare)])
        self.assertEqual(len(commit), 40)
        return bare, commit

    def listing(self):
        """Every path under the config and state folders, with its kind and the digest of a file's bytes."""
        found = []
        for top in (self.tmp / "config", self.state):
            if not os.path.lexists(top):
                continue
            for root, dirs, files in os.walk(top):
                dirs.sort()
                for name in sorted(dirs + files):
                    path = Path(root) / name
                    if path.is_symlink():
                        found.append((str(path), "link", os.readlink(path)))
                    elif path.is_file():
                        found.append((str(path), "file", hashlib.sha256(path.read_bytes()).hexdigest()))
                    else:
                        found.append((str(path), "dir", ""))
        return found

    def scratch_left(self):
        return [path.name for path in self.scratch.iterdir() if path.name.startswith("plugin-fetch-")]

    def run_cli(self, *argv):
        from recursion_core.plugin_security._entrypoints import main

        out, err = io.StringIO(), io.StringIO()
        status = main(["plugin", "install", *argv, "--config", str(self.config), "--state", str(self.state),
                       "--no-grant"], out=out, err=err, inp=io.StringIO(""))
        return status, out.getvalue(), err.getvalue()

    def assert_refused(self, code, *argv, words=()):
        before = self.listing()
        status, out, err = self.run_cli(*argv)
        self.assertEqual(status, 1, (out, err))
        self.assertIn(f"refused: {code}\n", err)
        for word in words:
            self.assertIn(word, err)
        self.assertEqual(self.listing(), before)
        self.assertEqual(self.scratch_left(), [])
        return err


@unittest.skipUnless(LINUX, LINUX_ONLY)
class InstallByIdTests(RegistryCase):
    def test_install_by_id_from_a_registry_at_a_full_commit(self):
        self.write_registry(_PLUGIN)
        bare, commit = self.commit_registry(extra={"README.md": b"A made-up registry.\n",
                                                   "docs/FORMAT.md": b"made-up format page\n"})
        status, out, err = self.run_cli(_PLUGIN, "--registry", f"{bare}@{commit}")
        self.assertEqual(status, 0, err)
        result = json.loads(out)
        self.assertEqual(result["plugin_id"], _PLUGIN)
        self.assertEqual(result["registry"]["commit"], commit)
        self.assertEqual(result["registry"]["address"], str(bare))
        self.assertTrue((self.config / "plugins" / _PLUGIN / "plugin.py").is_file())
        self.assertEqual(self.scratch_left(), [])

    def test_a_file_address_is_the_same_git_read(self):
        self.write_registry(_PLUGIN)
        bare, commit = self.commit_registry()
        status, out, err = self.run_cli(_PLUGIN, "--registry", f"file://{bare}@{commit}")
        self.assertEqual(status, 0, err)
        result = json.loads(out)
        self.assertEqual(result["registry"]["given"], f"file://{bare}@{commit}")
        self.assertEqual(result["registry"]["address"], str(bare))

    def test_the_id_may_be_given_with_the_older_flag(self):
        self.write_registry(_PLUGIN)
        bare, commit = self.commit_registry()
        status, _out, err = self.run_cli("--plugin", _PLUGIN, "--registry", f"{bare}@{commit}")
        self.assertEqual(status, 0, err)

    def test_the_registry_check_reads_the_fetched_tree(self):
        # A file the checker refuses, committed into the registry area: the install refuses the checker's code.
        self.write_registry(_PLUGIN)
        bare, commit = self.commit_registry(extra={"registry/stray.txt": b"not a registry file\n"})
        self.assert_refused("registry_unexpected_file", _PLUGIN, "--registry", f"{bare}@{commit}")


@unittest.skipUnless(LINUX, LINUX_ONLY)
class RefusalTests(RegistryCase):
    def test_no_registry_named_refuses_and_says_how_to_name_one(self):
        self.assert_refused("registry_not_named", _PLUGIN, words=("--registry <address>@<full commit>",))

    def test_a_branch_name_in_place_of_the_full_commit(self):
        self.write_registry(_PLUGIN)
        bare, _commit = self.commit_registry()
        self.assert_refused("registry_commit_refused", _PLUGIN, "--registry", f"{bare}@main",
                            words=("full commit",))

    def test_a_tag_in_place_of_the_full_commit(self):
        self.write_registry(_PLUGIN)
        bare, _commit = self.commit_registry(tag="v1")
        self.assert_refused("registry_commit_refused", _PLUGIN, "--registry", f"{bare}@v1")

    def test_a_short_commit_in_place_of_the_full_commit(self):
        self.write_registry(_PLUGIN)
        bare, commit = self.commit_registry()
        self.assert_refused("registry_commit_refused", _PLUGIN, "--registry", f"{bare}@{commit[:12]}")

    def test_an_address_with_no_commit(self):
        self.assert_refused("registry_commit_refused", _PLUGIN, "--registry", "https://example.invalid/registry")

    def test_an_address_of_a_scheme_this_install_does_not_read(self):
        self.assert_refused("registry_address_refused", _PLUGIN, "--registry",
                            "ssh://example.invalid/registry@" + "a" * 40)

    def test_a_registry_that_fails_the_checker(self):
        self.write_registry(_PLUGIN, index="empty")
        bare, commit = self.commit_registry()
        self.assert_refused("index_stale", _PLUGIN, "--registry", f"{bare}@{commit}")

    def test_an_unknown_plugin_id(self):
        self.write_registry(_PLUGIN)
        bare, commit = self.commit_registry()
        self.assert_refused("plugin_not_in_registry", "fixture.absent", "--registry", f"{bare}@{commit}")

    def test_an_unknown_version(self):
        self.write_registry(_PLUGIN)
        bare, commit = self.commit_registry()
        self.assert_refused("release_not_in_registry", _PLUGIN, "--registry", f"{bare}@{commit}",
                            "--version", "9.9.9")

    def test_a_link_anywhere_in_the_tree(self):
        self.write_registry(_PLUGIN)
        bare, commit = self.commit_registry(extra={"README.md": b"x\n"}, links={"docs/link.md": "../README.md"})
        self.assert_refused("registry_link_refused", _PLUGIN, "--registry", f"{bare}@{commit}",
                            words=("docs/link.md",))

    def test_a_submodule_anywhere_in_the_tree(self):
        self.write_registry(_PLUGIN)
        bare, commit = self.commit_registry(gitlink=True)
        self.assert_refused("registry_link_refused", _PLUGIN, "--registry", f"{bare}@{commit}",
                            words=("registry/sub",))

    def test_a_release_whose_runtime_this_daemon_does_not_run(self):
        entry = self.write_registry(_PLUGIN)
        compat = dict(entry["releases"][0]["compatibility"], runtime="wasm-sandbox")
        self.registry_rewrite(entry, compatibility=compat)
        bare, commit = self.commit_registry()
        self.assert_refused("release_compatibility", _PLUGIN, "--registry", f"{bare}@{commit}")

    def test_an_install_script_in_the_registry_tree(self):
        self.write_registry(_PLUGIN)
        bare, commit = self.commit_registry(extra={f"registry/{_PLUGIN}/1.0.0/install.sh": b"echo no\n"})
        self.assert_refused("registry_install_script", _PLUGIN, "--registry", f"{bare}@{commit}",
                            words=(f"registry/{_PLUGIN}/1.0.0/install.sh",))

    def test_a_registry_over_the_file_figure(self):
        from unittest.mock import patch

        self.write_registry(_PLUGIN)
        bare, commit = self.commit_registry()
        with patch("recursion_core.plugin_security.git_source._MAX_REGISTRY_FILES", 2):
            self.assert_refused("registry_too_large", _PLUGIN, "--registry", f"{bare}@{commit}",
                                words=("more files or more bytes",))

    def test_a_registry_over_the_byte_figure(self):
        from unittest.mock import patch

        self.write_registry(_PLUGIN)
        bare, commit = self.commit_registry()
        with patch("recursion_core.plugin_security.git_source._MAX_REGISTRY_BYTES", 16):
            self.assert_refused("registry_too_large", _PLUGIN, "--registry", f"{bare}@{commit}")

    def test_a_registry_address_that_does_not_answer(self):
        missing = self.tmp / "no-such-registry.git"
        self.assert_refused("registry_unreachable", _PLUGIN, "--registry", f"{missing}@{'b' * 40}",
                            words=("no other registry was tried", "Check the address"))

    def test_a_commit_the_registry_does_not_hold(self):
        self.write_registry(_PLUGIN)
        bare, _commit = self.commit_registry()
        self.assert_refused("registry_unreachable", _PLUGIN, "--registry", f"{bare}@{'c' * 40}")

    def registry_rewrite(self, entry, **release_over):
        from recursion_core.plugin_security.registry import build_index, index_bytes

        entry["releases"][0].update(release_over)
        (self.registry / "registry" / _PLUGIN / "entry.json").write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n")
        (self.registry / "registry" / "index.json").write_bytes(index_bytes(build_index([entry])))


if __name__ == "__main__":
    unittest.main()
