"""Install from a local bare git repository, then enable, disable and roll back.

The fetch uses the git program and a directory on this machine. No test passes a
network address to git. Each refusal below is one offending fixture: a short
commit, a tag, a branch name, ``..`` in the path, a symlink that leaves the
tree, an install script, and a commit whose file differs by one byte.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import threading
import unittest

from recursion_core.plugin_security.signing import backend_available
from recursion_core.plugin_security.tests.support import FakeGilProbe
from recursion_core.plugin_security.tests.test_plugin_cli_install import (
    LINUX, LINUX_ONLY, ProfileCase, _entry, _manifest, _sha,
)

_OUTSIDE = b"OUTSIDE-SECRET-BYTES\n"
_GOOD = b"VALUE = 1\n"
_BAD = b"VALUE = 2\n"


def _git(args, **kwargs):
    run = subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, **kwargs)
    return run.stdout


class GitCase(ProfileCase):
    def make_bare(self, files, links=None, *, tag=None):
        """Commit ``files`` and return ``(bare_path, full_commit)``. ``links`` are symlinks."""
        root = self.tmp / f"src-{len(list(self.tmp.iterdir()))}"
        work = root / "work"
        bare = root / "bare.git"
        work.mkdir(parents=True)
        _git(["init", "-q", "-b", "main", str(work)])
        for name, data in files.items():
            path = work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        for name, target in (links or {}).items():
            os.symlink(target, work / name)
        _git(["-C", str(work), "add", "-A"])
        _git(["-C", str(work), "-c", "user.name=plugin-fixture",
              "-c", "user.email=plugin-fixture@example.invalid", "commit", "-q", "-m", "plugin"])
        if tag:
            _git(["-C", str(work), "tag", tag])
        commit = _git(["-C", str(work), "rev-parse", "HEAD"]).strip()
        _git(["clone", "--bare", "-q", str(work), str(bare)])
        self.assertEqual(len(commit), 40)
        return bare, commit

    def write_unvendored(self, source, commit, *, path="plugin.py", plugin_id="fixture.unsigned"):
        """Registry release with no ``plugin.py``. The entry names ``commit`` and ``path``."""
        from recursion_core.plugin_security.registry import build_index, index_bytes

        manifest = _manifest(source, plugin_id, developer_key="11" * 32)
        entry = _entry(plugin_id, "1.0.0", manifest, source)
        entry["releases"][0]["source"] = {
            "repository": "https://example.invalid/fixtures/" + plugin_id,
            "commit": commit,
            "path": path,
        }
        plugin = self.registry / "registry" / plugin_id / "1.0.0"
        plugin.mkdir(parents=True, mode=0o700)
        (plugin / "manifest.json").write_bytes(manifest)
        (self.registry / "registry" / plugin_id / "entry.json").write_text(
            json.dumps(entry, indent=2, sort_keys=True) + "\n")
        (self.registry / "registry" / "index.json").write_bytes(index_bytes(build_index([entry])))
        return manifest

    def fetch(self, bare, commit, path, *, expected=None, scripts=()):
        from recursion_core.plugin_security.git_source import fetch_plugin_source

        return fetch_plugin_source(
            str(bare), commit, path, expected_sha256=expected or _sha(_GOOD), install_scripts=tuple(scripts))

    def install_git(self, bare, plugin_id="fixture.unsigned"):
        from recursion_core.plugin_security.install import install_plugin

        return install_plugin(
            self.config, self.registry, plugin_id, state=self.state, git_repository=str(bare))

    def refused(self, call):
        from recursion_core.plugin_security.git_source import SourceRefused
        from recursion_core.plugin_security.install import InstallRefused

        with self.assertRaises((SourceRefused, InstallRefused)) as caught:
            call()
        return caught.exception.code

    def assert_profile_untouched(self):
        self.assertFalse((self.config / "plugins").exists())
        self.assertFalse((self.config / "plugin-generations").exists())
        self.assertFalse((self.config / "plugin-install.lock").exists())

    def assert_no_scratch(self):
        root = Path(os.environ.get("TMPDIR", "/tmp"))
        if not root.is_dir():
            return
        left = [path for path in root.iterdir() if path.name.startswith("plugin-fetch-")]
        self.assertEqual(left, [], left)


@unittest.skipUnless(LINUX, LINUX_ONLY)
class GitRefusalTests(GitCase):
    """Each refusal is red without the check, and nothing is copied into the profile."""

    def test_an_abbreviated_commit_is_refused(self):
        bare, commit = self.make_bare({"plugin.py": _GOOD})
        code = self.refused(lambda: self.fetch(bare, commit[:12], "plugin.py"))
        self.assertEqual(code, "source_commit_refused")
        self.assert_no_scratch()

    def test_a_tag_is_refused_in_place_of_the_full_commit(self):
        bare, _commit = self.make_bare({"plugin.py": _GOOD}, tag="v1")
        code = self.refused(lambda: self.fetch(bare, "v1", "plugin.py"))
        self.assertEqual(code, "source_commit_refused")
        self.assert_no_scratch()

    def test_a_branch_name_is_refused_in_place_of_the_full_commit(self):
        bare, _commit = self.make_bare({"plugin.py": _GOOD})
        code = self.refused(lambda: self.fetch(bare, "main", "plugin.py"))
        self.assertEqual(code, "source_commit_refused")
        self.assert_no_scratch()

    def test_a_path_that_leaves_the_repository_is_refused(self):
        bare, commit = self.make_bare({"plugin.py": _GOOD})
        outside = self.tmp / "outside.py"
        outside.write_bytes(_OUTSIDE)
        for path in ("../outside.py", "sub/../../outside.py", "/tmp/plugin.py"):
            code = self.refused(lambda path=path: self.fetch(bare, commit, path))
            self.assertEqual(code, "source_path_escapes", path)
        self.assertEqual(outside.read_bytes(), _OUTSIDE)
        self.assert_no_scratch()

    def test_a_symlink_out_of_the_tree_is_refused_and_not_read(self):
        outside = self.tmp / "outside-secret"
        outside.write_bytes(_OUTSIDE)
        bare, commit = self.make_bare({}, links={"plugin.py": str(outside)})
        self.write_unvendored(_GOOD, commit)
        code = self.refused(lambda: self.install_git(bare))
        self.assertEqual(code, "source_symlink_escapes")
        self.assert_profile_untouched()
        self.assert_no_scratch()
        self.assertNotIn(_OUTSIDE, b"".join(path.read_bytes() for path in self.config.rglob("*")
                                             if path.is_file()) if self.config.exists() else b"")

    def test_an_install_script_is_refused_and_not_executed(self):
        marker = self.tmp / "install-script-ran"
        script = b"#!/bin/sh\n" + f"touch {marker}\n".encode()
        bare, commit = self.make_bare({"plugin.py": _GOOD, "install.sh": script})
        self.write_unvendored(_GOOD, commit)
        code = self.refused(lambda: self.install_git(bare))
        self.assertEqual(code, "source_install_script")
        self.assertFalse(marker.exists())
        self.assert_profile_untouched()
        self.assert_no_scratch()

    def test_a_commit_that_differs_by_one_byte_is_refused_before_copy(self):
        bare, commit = self.make_bare({"plugin.py": _BAD})
        self.write_unvendored(_GOOD, commit)
        code = self.refused(lambda: self.install_git(bare))
        self.assertEqual(code, "source_digest_mismatch")
        self.assert_profile_untouched()
        self.assert_no_scratch()
        self.assertFalse(any(self.config.rglob("plugin.py")) if self.config.exists() else False)

    def test_a_network_url_is_refused_without_calling_git(self):
        calls = []
        real = subprocess.Popen

        def boom(*args, **kwargs):
            calls.append(args)
            raise AssertionError("git was started for a network URL")

        subprocess.Popen = boom
        try:
            from recursion_core.plugin_security.git_source import fetch_plugin_source
            for url in ("https://example.invalid/plugin.git", "git@example.invalid:a/b.git",
                        "ssh://example.invalid/a/b.git", "file:///tmp/bare.git"):
                code = self.refused(lambda url=url: fetch_plugin_source(
                    url, "a" * 40, "plugin.py", expected_sha256="ab" * 32, install_scripts=()))
                self.assertEqual(code, "source_network_refused", url)
        finally:
            subprocess.Popen = real
        self.assertEqual(calls, [])
        self.assert_no_scratch()

    def test_a_hostile_repository_does_not_execute_its_hook_or_monitor(self):
        marker = self.tmp / "hostile-ran"
        bare, commit = self.make_bare({"plugin.py": _GOOD})
        hook = bare / "hooks" / "pre-upload-pack"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o700)
        (bare / "hooks" / "post-checkout").write_text(hook.read_text())
        (bare / "hooks" / "post-checkout").chmod(0o700)
        evil = self.tmp / "evil.sh"
        evil.write_text(f"#!/bin/sh\ntouch {marker}\nexec git pack-objects \"$@\"\n")
        evil.chmod(0o700)
        subprocess.run(["git", "-C", str(bare), "config", "core.fsmonitor", f"!touch {marker}"], check=True)
        subprocess.run(["git", "-C", str(bare), "config", "uploadpack.packObjectsHook", str(evil)], check=True)
        self.write_unvendored(_GOOD, commit)
        marker.unlink(missing_ok=True)
        result = self.install_git(bare)
        self.assertEqual(result["registry_label"], "non_verified")
        self.assertFalse(marker.exists())
        self.assert_no_scratch()


@unittest.skipUnless(LINUX, LINUX_ONLY)
class GitInstallTests(GitCase):
    def test_a_full_commit_from_a_local_bare_repository_installs_and_shows_the_label(self):
        bare, commit = self.make_bare({"plugin.py": _GOOD})
        self.write_unvendored(_GOOD, commit)
        modes = []
        real_mkdir = os.mkdir

        def record(path, mode=0o777):
            real_mkdir(path, mode)
            if "plugin-fetch-" in str(path):
                modes.append(stat.S_IMODE(os.lstat(path).st_mode))

        import socket
        opened = []

        def refuse_socket(*_args, **_kwargs):
            opened.append(1)
            raise AssertionError("install opened a socket")

        real_socket = socket.socket
        socket.socket = refuse_socket
        os.mkdir = record
        try:
            result = self.install_git(bare)
        finally:
            os.mkdir = real_mkdir
            socket.socket = real_socket
        self.assertEqual(opened, [])
        self.assertTrue(modes)
        self.assertTrue(all(mode == 0o700 for mode in modes), modes)
        self.assertEqual(result["registry_label"], "non_verified")
        self.assertEqual(result["trust"], "unsigned")
        self.assertTrue(result["fetched"])
        self.assertEqual((self.config / "plugins" / "fixture.unsigned" / "plugin.py").read_bytes(), _GOOD)
        self.assert_no_scratch()
        shown = self.listed()
        self.assertEqual(shown["plugins"], [{
            "plugin_id": "fixture.unsigned", "version": "1.0.0", "registry_label": "non_verified",
        }])
        if backend_available():
            from recursion_core.daemon.plugins import PluginHost
            host = PluginHost(self.config, self.state, gil_probe=FakeGilProbe())
            self.addCleanup(host.close)
            self.assertEqual(host.records[0]["state"], "loaded")
            self.assertEqual(host.records[0]["registry_label"], "non_verified")

    def test_a_missing_git_repository_is_still_not_fetched(self):
        bare, commit = self.make_bare({"plugin.py": _GOOD})
        del bare
        self.write_unvendored(_GOOD, commit)
        from recursion_core.plugin_security.install import InstallRefused, install_plugin
        with self.assertRaises(InstallRefused) as caught:
            install_plugin(self.config, self.registry, "fixture.unsigned", state=self.state)
        self.assertEqual(caught.exception.code, "source_unavailable")
        self.assert_profile_untouched()

    def test_scratch_is_removed_when_the_repository_is_not_bare(self):
        work = self.tmp / "not-bare"
        work.mkdir()
        _git(["init", "-q", "-b", "main", str(work)])
        (work / "plugin.py").write_bytes(_GOOD)
        _git(["-C", str(work), "add", "plugin.py"])
        _git(["-C", str(work), "-c", "user.name=plugin-fixture",
              "-c", "user.email=plugin-fixture@example.invalid", "commit", "-q", "-m", "plugin"])
        commit = _git(["-C", str(work), "rev-parse", "HEAD"]).strip()
        code = self.refused(lambda: self.fetch(work / ".git", commit, "plugin.py"))
        self.assertEqual(code, "source_not_local")
        self.assert_no_scratch()

    def test_a_second_caller_waits_and_both_fetches_leave_one_profile(self):
        bare, commit = self.make_bare({"plugin.py": _GOOD})
        self.write_unvendored(_GOOD, commit)
        from recursion_core.plugin_security import install as install_mod
        held = threading.Event()
        release = threading.Event()
        errors = []

        def hold():
            fd = install_mod._lock(self.config, wait=True)
            held.set()
            release.wait(5)
            os.close(fd)

        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(held.wait(5))
        from recursion_core.plugin_security.install import InstallRefused
        with self.assertRaises(InstallRefused) as caught:
            install_mod.install_plugin(
                self.config, self.registry, "fixture.unsigned", state=self.state,
                git_repository=str(bare), wait=False)
        self.assertEqual(caught.exception.code, "install_busy")
        self.assert_no_scratch()
        release.set()
        thread.join(5)

        def once():
            try:
                errors.append(self.install_git(bare)["generation"])
            except Exception as exc:  # noqa: BLE001 - the thread must report the failure
                errors.append(exc)

        workers = [threading.Thread(target=once) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(30)
        self.assertEqual(sorted(item for item in errors if isinstance(item, int)), [1, 2])
        self.assertEqual([row["plugin_id"] for row in self.listed()["plugins"]], ["fixture.unsigned"])
        self.assert_no_scratch()


@unittest.skipUnless(LINUX, LINUX_ONLY)
class EnableDisableRollbackTests(GitCase):
    def test_disable_hides_the_plugin_and_enable_brings_the_same_bytes_back(self):
        self.write_registry(source=_GOOD)
        self.install()
        from recursion_core.plugin_security.install import disable_plugin, enable_plugin
        hidden = disable_plugin(self.config, "fixture.unsigned", state=self.state)
        self.assertEqual(hidden["generation"], 2)
        self.assertFalse((self.config / "plugins" / "fixture.unsigned").exists())
        self.assertEqual(
            (self.config / "plugin-generations" / "2" / "fixture.unsigned" / "plugin.py").read_bytes(), _GOOD)
        self.assertEqual(self.listed()["plugins"], [{
            "plugin_id": "fixture.unsigned", "version": "1.0.0", "registry_label": "non_verified",
            "enabled": False,
        }])
        from recursion_core.daemon.plugins import PluginHost
        host = PluginHost(self.config, self.state, gil_probe=FakeGilProbe())
        self.addCleanup(host.close)
        self.assertEqual(host.records, [])
        host.close()
        shown = enable_plugin(self.config, "fixture.unsigned", state=self.state)
        self.assertEqual(shown["generation"], 3)
        self.assertEqual((self.config / "plugins" / "fixture.unsigned" / "plugin.py").read_bytes(), _GOOD)
        self.assertEqual(self.listed()["plugins"][0]["registry_label"], "non_verified")
        self.assertNotIn("enabled", self.listed()["plugins"][0])
        if backend_available():
            host = PluginHost(self.config, self.state, gil_probe=FakeGilProbe())
            self.addCleanup(host.close)
            self.assertEqual(host.records[0]["state"], "loaded")

    def test_disable_and_enable_of_a_missing_plugin_write_nothing(self):
        self.write_registry()
        self.install()
        current = (self.config / "plugin-generations" / "current").read_text()
        from recursion_core.plugin_security.install import InstallRefused, disable_plugin, enable_plugin
        with self.assertRaises(InstallRefused) as caught:
            disable_plugin(self.config, "fixture.missing", state=self.state)
        self.assertEqual(caught.exception.code, "plugin_not_installed")
        disable_plugin(self.config, "fixture.unsigned", state=self.state)
        with self.assertRaises(InstallRefused) as caught:
            disable_plugin(self.config, "fixture.unsigned", state=self.state)
        self.assertEqual(caught.exception.code, "plugin_already_disabled")
        enable_plugin(self.config, "fixture.unsigned", state=self.state)
        with self.assertRaises(InstallRefused) as caught:
            enable_plugin(self.config, "fixture.unsigned", state=self.state)
        self.assertEqual(caught.exception.code, "plugin_not_disabled")
        # The refusals did not take another generation. Install, disable and enable are 1, 2 and 3.
        self.assertEqual((self.config / "plugin-generations" / "current").read_text(), "3\n")
        self.assertNotEqual(current, "3\n")
        self.assertFalse((self.config / "plugin-generations" / "4").exists())

    def test_rollback_points_at_the_previous_generation(self):
        self.write_registry(source=_GOOD)
        self.install()
        other = self.tmp / "other-root"
        saved = self.registry
        self.registry = other
        self.write_registry(plugin_id="fixture.beta", source=b"VALUE = 2\n")
        self.install("fixture.beta")
        self.registry = saved
        from recursion_core.plugin_security.install import InstallRefused, rollback_generation
        rolled = rollback_generation(self.config, state=self.state)
        self.assertEqual(rolled["generation"], 1)
        self.assertEqual([row["plugin_id"] for row in rolled["plugins"]], ["fixture.unsigned"])
        self.assertEqual((self.config / "plugins" / "fixture.unsigned" / "plugin.py").read_bytes(), _GOOD)
        self.assertFalse((self.config / "plugins" / "fixture.beta").exists())
        self.assertTrue((self.config / "plugin-generations" / "2" / "fixture.beta" / "plugin.py").is_file())
        with self.assertRaises(InstallRefused) as caught:
            rollback_generation(self.config, state=self.state)
        self.assertEqual(caught.exception.code, "rollback_unavailable")
        self.assertEqual((self.config / "plugin-generations" / "current").read_text(), "1\n")
        # ``other`` is the registry that still names fixture.beta. The saved root does not.
        self.registry = other
        self.install("fixture.beta")
        self.assertEqual((self.config / "plugin-generations" / "current").read_text(), "3\n")
        self.assertEqual(
            [row["plugin_id"] for row in self.listed()["plugins"]],
            ["fixture.beta", "fixture.unsigned"],
        )

    def test_a_rollback_that_stops_while_publishing_is_finished_by_the_next_list(self):
        self.write_registry(source=_GOOD)
        self.install()
        other = self.tmp / "other-root"
        self.registry = other
        self.write_registry(plugin_id="fixture.beta", source=b"VALUE = 2\n")
        self.install("fixture.beta")
        from recursion_core.plugin_security import install as install_mod
        real = install_mod._copy_generation_to_live

        def boom(*_args, **_kwargs):
            raise OSError("interrupted")

        install_mod._copy_generation_to_live = boom
        try:
            with self.assertRaises(OSError):
                install_mod.rollback_generation(self.config, state=self.state)
        finally:
            install_mod._copy_generation_to_live = real
        self.assertEqual((self.config / "plugin-generations" / "current").read_text(), "1\n")
        shown = self.listed()
        self.assertEqual([row["plugin_id"] for row in shown["plugins"]], ["fixture.unsigned"])
        self.assertFalse((self.config / "plugins" / "fixture.beta").exists())

    def test_cli_enable_disable_and_rollback(self):
        self.write_registry(source=_GOOD)
        from recursion_core.plugin_security._entrypoints import main
        import io
        out, err = io.StringIO(), io.StringIO()
        status = main(["plugin", "install", "--registry", str(self.registry), "--plugin", "fixture.unsigned",
                       "--config", str(self.config), "--state", str(self.state)], out, err)
        self.assertEqual(status, 0, err.getvalue())
        out, err = io.StringIO(), io.StringIO()
        status = main(["plugin", "disable", "--plugin", "fixture.unsigned", "--config", str(self.config),
                       "--state", str(self.state)], out, err)
        self.assertEqual(status, 0, err.getvalue())
        self.assertFalse((self.config / "plugins" / "fixture.unsigned").exists())
        out, err = io.StringIO(), io.StringIO()
        status = main(["plugin", "enable", "--plugin", "fixture.unsigned", "--config", str(self.config),
                       "--state", str(self.state)], out, err)
        self.assertEqual(status, 0, err.getvalue())
        self.assertTrue((self.config / "plugins" / "fixture.unsigned" / "plugin.py").is_file())
        out, err = io.StringIO(), io.StringIO()
        status = main(["plugin", "rollback", "--config", str(self.config), "--state", str(self.state)], out, err)
        self.assertEqual(status, 0, err.getvalue())
        # Enable wrote generation 3. Rollback points at 2, the disabled generation.
        self.assertEqual(json.loads(out.getvalue())["generation"], 2)
        self.assertFalse((self.config / "plugins" / "fixture.unsigned").exists())
        out, err = io.StringIO(), io.StringIO()
        status = main(["plugin", "rollback", "--config", str(self.config), "--state", str(self.state)], out, err)
        self.assertEqual(status, 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["generation"], 1)
        self.assertTrue((self.config / "plugins" / "fixture.unsigned" / "plugin.py").is_file())
        out, err = io.StringIO(), io.StringIO()
        status = main(["plugin", "rollback", "--config", str(self.config), "--state", str(self.state)], out, err)
        self.assertEqual(status, 1)
        self.assertIn("refused: rollback_unavailable", err.getvalue())
        bare, commit = self.make_bare({"plugin.py": _GOOD})
        self.registry = self.tmp / "git-root"
        self.write_unvendored(_GOOD, commit, plugin_id="fixture.git")
        # The id grammar allows letters, digits, dot, underscore and hyphen. "fixture.git" is legal.
        out, err = io.StringIO(), io.StringIO()
        status = main(["plugin", "install", "--registry", str(self.registry), "--plugin", "fixture.git",
                       "--git", str(bare), "--config", str(self.config), "--state", str(self.state)], out, err)
        self.assertEqual(status, 0, err.getvalue())
        self.assertTrue(json.loads(out.getvalue())["fetched"])
        out, err = io.StringIO(), io.StringIO()
        status = main(["plugin", "install", "--registry", str(self.registry), "--plugin", "fixture.git",
                       "--git", "https://example.invalid/plugin.git", "--config", str(self.config),
                       "--state", str(self.state)], out, err)
        self.assertEqual(status, 1)
        self.assertIn("refused: source_network_refused", err.getvalue())


if __name__ == "__main__":
    unittest.main()
