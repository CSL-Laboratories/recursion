"""CSL-732 (B10, R2a): grant, launch plan, refusal codes and re-verify (candidate tests).

Run from the repository root:
``PYTHONPATH=core python3 -B -m unittest discover -s core/recursion_core/launch_check/tests -t core -v``.
Every binary is a fake built in a temp dir (tests/fixtures.py). No real engine, provider,
account, credential or network is used; every secret-shaped value below is fake.
"""

from __future__ import annotations

import dataclasses
import importlib
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from recursion_core.launch_check import (
    FLOORS,
    MAX_ARG_BYTES,
    MAX_SPAWN_BYTES,
    PROBE_ENV_BASE_NAMES,
    REFUSAL_CODES,
    REFUSAL_CODES_VERSION,
    Grant,
    LaunchRefused,
    grant,
    launch_plan,
    probe_env,
)
from recursion_core.launch_check import floors as floors_mod
from recursion_core.launch_check.probe import ProbeResult
from recursion_core.launch_check.tests.fixtures import Factory

# The package re-exports the grant() function under the module's name; fetch the module itself.
grant_mod = importlib.import_module("recursion_core.launch_check.grant")

N = 3  # repetitions per boundary cell
F: Factory


def setUpModule() -> None:
    global F
    F = Factory()


def tearDownModule() -> None:
    F.cleanup()


CLAUDE = "2.1.318 (Claude Code)"
CODEX = "codex-cli 0.160.0"
EXPECTED_CODES = {
    "engine_unknown", "path_not_absolute", "binary_missing", "wrapper_refused", "name_mismatch",
    "version_probe_failed", "binary_changed", "engine_mismatch", "version_unparsed", "floor_unset",
    "version_refused", "argv_empty", "argv0_mismatch", "grant_forged", "grant_stale", "spawn_too_large",
}
needs_exec = unittest.skipUnless(os.name == "posix", "POSIX fixtures")


class Base(unittest.TestCase):
    def refused(self, code: str, fn, *a, **kw) -> LaunchRefused:
        with self.assertRaises(LaunchRefused) as cm:
            fn(*a, **kw)
        self.assertEqual(code, cm.exception.code, cm.exception)
        self.assertIn(cm.exception.code, REFUSAL_CODES)
        return cm.exception

    def g(self, engine: str, path: str, **kw) -> Grant:
        kw.setdefault("probe", F.probe)
        return grant(engine, path, **kw)

    def claude(self, line: str = CLAUDE, **kw) -> str:
        return F.make("claude", line, **kw)


class RefusalCodeTests(Base):
    def test_closed_set_is_exactly_the_listed_codes(self) -> None:
        self.assertEqual(EXPECTED_CODES, set(REFUSAL_CODES))
        self.assertIsInstance(REFUSAL_CODES, frozenset)
        self.assertTrue(REFUSAL_CODES_VERSION.startswith("r2a-refusals/"))

    def test_unknown_code_cannot_be_raised(self) -> None:
        with self.assertRaises(ValueError):
            LaunchRefused("stopped", "claude_code", "")

    def test_record_carries_code_and_table_versions(self) -> None:
        rec = LaunchRefused("floor_unset", "grok", "x").record()
        self.assertEqual("refused", rec["outcome"])
        self.assertEqual(floors_mod.FLOOR_TABLE_VERSION, rec["floor_table"])
        self.assertEqual(REFUSAL_CODES_VERSION, rec["refusal_codes"])


@needs_exec
class GrantTests(Base):
    def test_claude_real_shape_line_grants_with_full_record(self) -> None:
        p = self.claude()
        g = self.g("claude_code", p)
        rec = g.record()
        for key in ("path", "realpath", "sha256", "size", "version_raw", "exit_code", "engine", "floor_table"):
            self.assertIn(key, rec)
        self.assertEqual(64, len(g.sha256))
        self.assertEqual(os.path.getsize(p), g.size)
        self.assertEqual(CLAUDE, g.version_raw)
        self.assertEqual("2.1.318", g.version)
        self.assertEqual(0, g.exit_code)
        self.assertEqual(("2.1.314", "2.1.260"), (g.floor_functional, g.floor_security))
        self.assertFalse(rec["beta"])
        self.assertNotIn("mac", rec)

    def test_codex_real_shape_line_grants(self) -> None:
        g = self.g("codex", F.make("codex", CODEX))
        self.assertEqual("0.160.0", g.version)
        self.assertEqual((None, "0.131.0"), (g.floor_functional, g.floor_security))
        self.assertTrue(g.record()["beta"])

    def test_floor_boundaries(self) -> None:
        cells = [
            ("claude_code", "claude", "2.1.313 (Claude Code)", "version_refused"),
            ("claude_code", "claude", "2.1.314 (Claude Code)", None),
            ("claude_code", "claude", "2.1.315 (Claude Code)", None),
            ("claude_code", "claude", "2.2.0 (Claude Code)", None),
            ("claude_code", "claude", "3.0.0 (Claude Code)", None),
            ("claude_code", "claude", "2.1.259 (Claude Code)", "version_refused"),
            ("claude_code", "claude", "1.0.400 (Claude Code)", "version_refused"),
            ("claude_code", "claude", "2.1.318-beta.1 (Claude Code)", "version_refused"),
            ("codex", "codex", "codex-cli 0.130.9", "version_refused"),
            ("codex", "codex", "codex-cli 0.131.0", None),
            ("codex", "codex", "codex-cli 0.145.0", None),
            ("codex", "codex", "codex-cli 0.167.0", None),
            ("codex", "codex", "codex-cli 0.131.0-alpha.2", "version_refused"),
            ("opencode", "opencode", "opencode v2.2.5", None),
            ("opencode", "opencode2", "opencode v2.2.5", None),
            ("opencode", "opencode", "1.18.32", "version_refused"),
            ("opencode", "opencode", "1.18.33", None),
            ("cursor", "agent", "2026.10.22", None),
            ("cursor", "cursor-agent", "2026.10.01-e373342", None),
            ("cursor", "agent", "2025.09.16-abcdef0", "version_refused"),
            ("cursor", "agent", "2025.09.17-25b418f", None),
            ("gemini", "gemini", "0.39.0", "version_refused"),
            ("gemini", "gemini", "0.39.1", None),
            ("grok", "grok", "grok 1.0.0", None),
            ("antigravity", "agy", "1.2.3", None),
            ("pi", "pi", "0.30.2", None),
        ]
        for engine, name, line, want in cells:
            for i in range(N):
                with self.subTest(engine=engine, line=line, i=i):
                    p = F.make(name, line)
                    if want is None:
                        g = self.g(engine, p)
                        self.assertEqual(engine, g.engine)
                        rec = g.record()
                        if engine == "claude_code":
                            self.assertFalse(rec["beta"])
                        else:
                            self.assertTrue(rec["beta"], engine)
                    else:
                        self.refused(want, self.g, engine, p)

    def test_security_refusal_names_the_security_floor(self) -> None:
        r = self.refused("version_refused", self.g, "claude_code", self.claude("2.1.259 (Claude Code)"))
        self.assertIn("security", r.detail)

    def test_hermes_reads_version_from_hermes_acp(self) -> None:
        d = F.dir()
        F.make("hermes", "", where=d)
        F.make("hermes-acp", "hermes-acp 0.9.1", where=d)
        g = self.g("hermes", os.path.join(d, "hermes"))
        self.assertTrue(g.record()["beta"])
        self.assertEqual("0.9.1", g.version)
        table = dict(floors_mod.FLOORS)
        table["hermes"] = dataclasses.replace(table["hermes"], functional="0.9.0")
        with mock.patch.object(floors_mod, "FLOORS", table):
            g = self.g("hermes", os.path.join(d, "hermes"))
            self.assertFalse(g.record()["beta"])
            self.assertEqual(os.path.join(d, "hermes-acp"), g.probe_path)
            self.assertNotEqual(g.sha256, g.probe_sha256)
            self.assertEqual("hermes-acp 0.9.1", g.version_raw)
            plan = launch_plan(g, ["hermes", "acp"])
            F.make("hermes-acp", "hermes-acp 0.9.1", where=d)  # swapped probe binary
            self.refused("binary_changed", plan.exec_argv)

    def test_hermes_without_hermes_acp_is_missing(self) -> None:
        d = F.dir()
        F.make("hermes", "hermes 0.9.1", where=d)
        self.refused("binary_missing", self.g, "hermes", os.path.join(d, "hermes"))

    def test_grant_is_immutable(self) -> None:
        g = self.g("claude_code", self.claude())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            g.sha256 = "0" * 64  # type: ignore[misc]


@needs_exec
class GrantRefusalTests(Base):
    def test_engine_unknown(self) -> None:
        p = self.claude()
        for engine in ("claude", "kiro", "", "CLAUDE_CODE", None, 7):
            with self.subTest(engine=engine):
                self.refused("engine_unknown", self.g, engine, p)

    def test_path_not_absolute(self) -> None:
        for p in ("claude", "./claude", "bin/claude", "", None):
            with self.subTest(p=p):
                self.refused("path_not_absolute", self.g, "claude_code", p)

    def test_binary_missing(self) -> None:
        d = F.dir()
        self.refused("binary_missing", self.g, "claude_code", os.path.join(d, "claude"))
        os.symlink(os.path.join(d, "gone"), os.path.join(d, "claude"))  # dangling link
        self.refused("binary_missing", self.g, "claude_code", os.path.join(d, "claude"))
        os.makedirs(os.path.join(d, "x", "claude"))  # a directory
        self.refused("binary_missing", self.g, "claude_code", os.path.join(d, "x", "claude"))

    def test_non_executable_native_is_missing(self) -> None:
        p = self.claude()
        os.chmod(p, 0o644)
        self.refused("binary_missing", self.g, "claude_code", p)

    def test_wrapper_refused_at_grant(self) -> None:
        d = F.dir()
        script = F.script(os.path.join(d, "claude"))
        self.refused("wrapper_refused", self.g, "claude_code", script)
        # symlink named claude -> script
        d2 = F.dir()
        target = F.script(os.path.join(d2, "real-claude"))
        os.symlink(target, os.path.join(d2, "claude"))
        self.refused("wrapper_refused", self.g, "claude_code", os.path.join(d2, "claude"))
        # symlink named codex -> codex.js
        d3 = F.dir()
        js = F.script(os.path.join(d3, "codex.js"), "#!/usr/bin/env node\n")
        os.symlink(js, os.path.join(d3, "codex"))
        self.refused("wrapper_refused", self.g, "codex", os.path.join(d3, "codex"))
        # plain text and zero-byte files carry no native magic
        d4 = F.dir()
        with open(os.path.join(d4, "claude"), "w") as f:
            f.write("2.1.318 (Claude Code)\n")
        os.chmod(os.path.join(d4, "claude"), 0o755)
        self.refused("wrapper_refused", self.g, "claude_code", os.path.join(d4, "claude"))
        d5 = F.dir()
        open(os.path.join(d5, "claude"), "w").close()
        os.chmod(os.path.join(d5, "claude"), 0o755)
        self.refused("wrapper_refused", self.g, "claude_code", os.path.join(d5, "claude"))
        # native file whose resolved name is an interpreter
        d6 = F.dir()
        node = F.make("node", CLAUDE, where=d6)
        os.symlink(node, os.path.join(d6, "claude"))
        self.refused("wrapper_refused", self.g, "claude_code", os.path.join(d6, "claude"))
        # wrapper-shaped grant names
        for name in ("claude.cmd", "claude.ps1", "claude.sh", "claude.js", "node", "env", "npx"):
            with self.subTest(name=name):
                p = F.make(name, CLAUDE)
                self.refused("wrapper_refused", self.g, "claude_code", p)

    def test_name_mismatch(self) -> None:
        for name in ("claudex", "codex", "Claude", "claude-code", "agent"):
            with self.subTest(name=name):
                self.refused("name_mismatch", self.g, "claude_code", F.make(name, CLAUDE))
        self.refused("name_mismatch", self.g, "cursor", F.make("cursor", "2026.10.22"))

    def test_version_probe_failed_on_exit_code(self) -> None:
        for code in (1, 2, 75, 127):
            with self.subTest(code=code):
                self.refused("version_probe_failed", self.g, "claude_code", self.claude(code=code))

    def test_version_probe_failed_on_timeout(self) -> None:
        if not F.compiled:
            self.skipTest("needs a compiled fixture")
        p = self.claude(sleep_ms=3000)
        r = self.refused("version_probe_failed", self.g, "claude_code", p, timeout=0.3)
        self.assertIn("exceeded", r.detail)

    def test_binary_changed_during_probe(self) -> None:
        p = self.claude()

        def swapping_probe(spec, realpath, timeout):
            F.write(p, CLAUDE)  # same line, new bytes
            return ProbeResult(CLAUDE, 0, 1)

        self.refused("binary_changed", grant, "claude_code", p, probe=swapping_probe)

    def test_engine_mismatch(self) -> None:
        self.refused("engine_mismatch", self.g, "claude_code", self.claude(CODEX))
        self.refused("engine_mismatch", self.g, "codex", F.make("codex", CLAUDE))
        self.refused("engine_mismatch", self.g, "opencode", F.make("opencode", "gemini 0.40.0"))
        self.refused("engine_mismatch", self.g, "hermes", self._hermes("codex-cli 0.160.0"))

    def _hermes(self, acp_line: str) -> str:
        d = F.dir()
        F.make("hermes", "", where=d)
        F.make("hermes-acp", acp_line, where=d)
        return os.path.join(d, "hermes")

    def test_version_unparsed(self) -> None:
        for line in ("", "Claude Code", "2.1 (Claude Code)", "2.1.318.4 (Claude Code)", "2.1.318abc (Claude Code)",
                     "2.1.318+build.5 (Claude Code)", "v02.1.318 (Claude Code)", "2.1.x (Claude Code)",
                     "2.1.318" + " " * 300 + "(Claude Code)"):
            with self.subTest(line=line):
                self.refused("version_unparsed", self.g, "claude_code", self.claude(line))
        for line in ("2026.13.01", "2026.10.22-zz", "26.10.22", "2026.10.22.1"):
            with self.subTest(line=line):
                self.refused("version_unparsed", self.g, "cursor", F.make("agent", line))

    def test_version_line_from_stderr_when_stdout_empty(self) -> None:
        if not F.compiled:
            self.skipTest("needs a compiled fixture")
        g = self.g("claude_code", self.claude("", stderr="2.1.320 (Claude Code)\n"))
        self.assertEqual("2.1.320", g.version)

    def test_no_grant_after_any_error(self) -> None:
        # Each refusal is an exception; grant() has no error-returning path.
        for line in ("2.1.313 (Claude Code)", CODEX, "", "2.1.318-rc.1 (Claude Code)"):
            with self.subTest(line=line):
                try:
                    out = self.g("claude_code", self.claude(line))
                except LaunchRefused:
                    out = None
                self.assertIsNone(out)


@needs_exec
class LaunchPlanTests(Base):
    def setUp(self) -> None:
        self.path = self.claude()
        self.grant = self.g("claude_code", self.path)

    def test_plan_execs_realpath_with_bare_argv0(self) -> None:
        plan = launch_plan(self.grant, ["claude", "-p", "hello"], {"HOME": "/tmp/x"})
        self.assertEqual([self.grant.realpath, "-p", "hello"], plan.exec_argv())
        self.assertEqual({"HOME": "/tmp/x"}, plan.exec_env())

    def test_argv_empty(self) -> None:
        for argv in ([], (), None):
            with self.subTest(argv=argv):
                self.refused("argv_empty", launch_plan, self.grant, argv)

    def test_argv0_mismatch(self) -> None:
        for a0 in (self.path, self.grant.realpath, "codex", "claude.exe", "./claude", "Claude", "claude "):
            with self.subTest(a0=a0):
                self.refused("argv0_mismatch", launch_plan, self.grant, [a0, "-p"])

    def test_wrapper_argv0_refused(self) -> None:
        for a0 in ("sh", "bash", "env", "node", "python", "python3", "python3.12", "npx", "uv", "cmd", "cmd.exe",
                   "powershell", "pwsh", "pwsh.exe", "wsl", "/usr/bin/env", "/bin/sh", "claude.cmd", "claude.ps1",
                   "claude.js", "claude.sh", "run.bat", "sudo", "busybox"):
            with self.subTest(a0=a0):
                self.refused("wrapper_refused", launch_plan, self.grant, [a0, "claude", "-p"])

    def test_spawn_too_large(self) -> None:
        big = "a" * MAX_ARG_BYTES
        self.refused("spawn_too_large", launch_plan, self.grant, ["claude", big])
        many = ["claude"] + ["b" * 60_000] * 5
        self.assertGreater(sum(len(a) for a in many), MAX_SPAWN_BYTES)
        self.refused("spawn_too_large", launch_plan, self.grant, many)
        self.refused("spawn_too_large", launch_plan, self.grant, ["claude"], {"X": "c" * MAX_ARG_BYTES})
        self.refused("spawn_too_large", launch_plan, self.grant, ["claude"],
                     {f"V{i}": "d" * 30_000 for i in range(10)})
        self.refused("spawn_too_large", launch_plan, self.grant, ["claude", "a\x00b"])
        self.refused("spawn_too_large", launch_plan, self.grant, ["claude"], {"A=B": "c"})
        self.refused("spawn_too_large", launch_plan, self.grant, ["claude", 7])
        launch_plan(self.grant, ["claude", "a" * (MAX_ARG_BYTES - 2)])  # just under the per-item cap

    def test_grant_forged(self) -> None:
        g = self.grant
        fakes = [
            g.record(),
            types.SimpleNamespace(**dataclasses.asdict(g)),
            dataclasses.replace(g, version="9.9.9"),
            dataclasses.replace(g, sha256="0" * 64),
            dataclasses.replace(g, realpath="/bin/sh"),
            dataclasses.replace(g, mac=""),
            dataclasses.replace(g, mac="0" * 64),
            Grant(**{**dataclasses.asdict(g), "mac": g.mac.upper()}),
            None,
        ]
        for fake in fakes:
            with self.subTest(fake=type(fake).__name__):
                self.refused("grant_forged", launch_plan, fake, ["claude"])
        tampered = dataclasses.replace(g)
        object.__setattr__(tampered, "path", "/usr/bin/claude")
        self.refused("grant_forged", launch_plan, tampered, ["claude"])

    def test_subclass_of_grant_is_forged(self) -> None:
        class Sub(Grant):
            pass

        self.refused("grant_forged", launch_plan, Sub(**dataclasses.asdict(self.grant)), ["claude"])

    def test_grant_from_another_process_key_is_forged(self) -> None:
        with mock.patch.object(grant_mod, "_GRANT_KEY", b"k" * 32):
            other = self.g("claude_code", self.path)
        self.refused("grant_forged", launch_plan, other, ["claude"])

    def test_grant_stale_after_table_change(self) -> None:
        plan = launch_plan(self.grant, ["claude"])
        with mock.patch.object(floors_mod, "FLOOR_TABLE_VERSION", "r2a-floors/1.0.1-test"):
            self.refused("grant_stale", launch_plan, self.grant, ["claude"])
            self.refused("grant_stale", plan.exec_argv)
        with mock.patch.object(grant_mod, "REFUSAL_CODES_VERSION", "r2a-refusals/9-test"):
            self.refused("grant_stale", launch_plan, self.grant, ["claude"])

    def test_floor_raised_after_grant_refuses_plan(self) -> None:
        g = self.g("claude_code", self.claude("2.1.314 (Claude Code)"))
        table = dict(floors_mod.FLOORS)
        table["claude_code"] = dataclasses.replace(table["claude_code"], functional="2.1.316")
        with mock.patch.object(floors_mod, "FLOORS", table):
            self.refused("version_refused", launch_plan, g, ["claude"])

    def test_unset_functional_floor_grants_as_beta_and_reaches_a_plan(self) -> None:
        g = self.g("grok", F.make("grok", "grok 1.0.0"))
        self.assertTrue(g.record()["beta"])
        plan = launch_plan(g, ["grok"])
        self.assertEqual(g.realpath, plan.exec_argv()[0])


@needs_exec
class ReverifyTests(Base):
    """Re-verify on every plan use (C1-37): full re-hash and resolution, not a stat key."""

    def shim(self) -> tuple[str, str]:
        d = F.dir()
        os.makedirs(os.path.join(d, "bin"))
        os.makedirs(os.path.join(d, "versions"))
        target = F.make("2.1.318", CLAUDE, where=os.path.join(d, "versions"))
        link = os.path.join(d, "bin", "claude")
        os.symlink(target, link)
        return link, target

    def test_plan_reuse_reverifies_every_time(self) -> None:
        link, target = self.shim()
        g = self.g("claude_code", link)
        self.assertEqual(target, g.realpath)
        plan = launch_plan(g, ["claude", "-p"])
        for _ in range(5):
            self.assertEqual([target, "-p"], plan.exec_argv())
        with open(target, "rb") as f:
            original = f.read()
        F.write(target, CLAUDE)
        for _ in range(N):
            self.refused("binary_changed", plan.exec_argv)
        self.refused("binary_changed", launch_plan, g, ["claude"])
        with open(target, "wb") as f:  # identity is the digest: restoring the bytes restores the grant
            f.write(original)
        self.assertEqual([target, "-p"], plan.exec_argv())

    def test_swapped_binary_behind_unchanged_symlink_shim(self) -> None:
        link, target = self.shim()
        g = self.g("claude_code", link)
        plan = launch_plan(g, ["claude"])
        before = os.readlink(link)
        os.replace(F.make("x", CLAUDE), target)  # rename-over; the link is untouched
        self.assertEqual(before, os.readlink(link))
        self.refused("binary_changed", plan.exec_argv)

    def test_symlink_retargeted(self) -> None:
        link, _ = self.shim()
        plan = launch_plan(self.g("claude_code", link), ["claude"])
        other = F.make("2.1.319", "2.1.319 (Claude Code)")
        os.unlink(link)
        os.symlink(other, link)
        self.refused("binary_changed", plan.exec_argv)

    def test_same_size_rewrite_with_mtime_restored(self) -> None:
        p = self.claude()
        st = os.stat(p)
        plan = launch_plan(self.g("claude_code", p), ["claude"])
        with open(p, "r+b") as f:  # in place: same inode, same size
            f.write(F.content(CLAUDE))
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
        st2 = os.stat(p)
        self.assertEqual((st.st_size, st.st_mtime_ns, st.st_ino), (st2.st_size, st2.st_mtime_ns, st2.st_ino))
        self.refused("binary_changed", plan.exec_argv)

    def test_deleted_binary_is_missing(self) -> None:
        link, target = self.shim()
        plan = launch_plan(self.g("claude_code", link), ["claude"])
        os.unlink(target)
        self.refused("binary_missing", plan.exec_argv)
        p = self.claude()
        plan2 = launch_plan(self.g("claude_code", p), ["claude"])
        os.unlink(p)
        self.refused("binary_missing", plan2.exec_argv)
        self.refused("binary_missing", launch_plan, plan2.grant, ["claude"])

    def test_link_deleted_is_missing(self) -> None:
        link, _ = self.shim()
        plan = launch_plan(self.g("claude_code", link), ["claude"])
        os.unlink(link)
        self.refused("binary_missing", plan.exec_argv)


@needs_exec
class ZeroEgressProbeTests(Base):
    POISON = {
        "HTTPS_PROXY": "http://proxy.invalid:3128", "https_proxy": "http://proxy.invalid:3128",
        "HTTP_PROXY": "http://proxy.invalid:3128", "http_proxy": "http://proxy.invalid:3128",
        "ALL_PROXY": "socks5://proxy.invalid:1080", "all_proxy": "socks5://proxy.invalid:1080",
        "NO_PROXY": "*", "ANTHROPIC_API_KEY": "fake-not-a-key", "OPENAI_API_KEY": "fake-not-a-key",
        "CLAUDE_CODE_OAUTH_TOKEN": "fake", "CODEX_HOME": "/tmp/fake-codex-home", "GITHUB_TOKEN": "fake",
        "GH_TOKEN": "fake", "AWS_SECRET_ACCESS_KEY": "fake", "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/fake.json",
        "GEMINI_API_KEY": "fake", "XAI_API_KEY": "fake", "CLI_VERSION": "9.9.9", "NODE_OPTIONS": "--require x",
        "LD_PRELOAD": "/tmp/fake.so", "SSH_AUTH_SOCK": "/tmp/fake.sock", "SSL_CERT_FILE": "/tmp/fake.pem",
    }

    def test_probe_env_is_built_from_nothing(self) -> None:
        with mock.patch.dict(os.environ, self.POISON):
            for spec in FLOORS.values():
                env = probe_env(spec, "/tmp/h")
                with self.subTest(engine=spec.engine):
                    self.assertLessEqual(set(env) - {k for k, _ in spec.probe_env}, PROBE_ENV_BASE_NAMES)
                    self.assertFalse(set(env) & set(self.POISON))
                    self.assertEqual("/tmp/h", env["HOME"])
                    self.assertEqual("/usr/bin:/bin", env["PATH"])
        self.assertEqual("1", probe_env(FLOORS["claude_code"], "/h")["DISABLE_UPDATES"])

    def test_probe_child_receives_no_proxy_or_credential_vars(self) -> None:
        if not F.compiled:
            self.skipTest("needs a compiled fixture")
        out = tempfile.mkdtemp(prefix="csl732-dump-")
        self.addCleanup(shutil.rmtree, out, True)
        dump = os.path.join(out, "env.txt")
        for engine, name, line in (("claude_code", "claude", CLAUDE), ("codex", "codex", CODEX)):
            with self.subTest(engine=engine), mock.patch.dict(os.environ, self.POISON):
                p = F.make(name, line, dump=dump)
                g = grant(engine, p)
                with open(dump) as f:
                    text = f.read()
                env_part, rest = text.split("--argv--\n")
                argv_part, stdin_part = rest.split("--stdin--\n")
                env = dict(item.split("=", 1) for item in env_part.splitlines())
                self.assertFalse(set(env) & set(self.POISON), env)
                for k in env:
                    self.assertTrue(k in PROBE_ENV_BASE_NAMES or k in dict(FLOORS[engine].probe_env), k)
                self.assertFalse(any(v in "\n".join(env.values()) for v in ("proxy.invalid", "fake-not-a-key")))
                self.assertNotEqual(os.path.expanduser("~"), env["HOME"])
                self.assertFalse(os.path.exists(env["HOME"]), "probe home is removed")
                self.assertEqual([g.realpath, "--version"], argv_part.split())
                self.assertEqual("/dev/null", stdin_part.strip())


if __name__ == "__main__":
    unittest.main()
