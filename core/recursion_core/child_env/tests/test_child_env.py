"""CSL-785: child-environment allowlist and launch boundary in the new core (candidate tests).

Run from the repository root: ``PYTHONPATH=core python3 -B -m unittest discover -s core/recursion_core/child_env/tests -t core -v``.
No engine, provider, network or real credential is used; every secret-shaped value is fake.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from recursion_core.child_env import BASE_NAMES, ENGINE_PROFILES, EnvRefused, ValidatedEnv, build_child_env, spawn
from recursion_core.child_env.policy import CLAUDE_TLS_NAMES
from recursion_core.child_env.tests import cells


def _running_as_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


FAKE = "fake-value-not-a-credential"


def _home(test: unittest.TestCase) -> str:
    home = cells.owned_tmpdir("csl785-home-")  # its chain is owned whatever TMPDIR names
    test.addCleanup(shutil.rmtree, home, True)
    return home


@unittest.skipIf(os.name == "nt", "POSIX rules")
class CellTests(unittest.TestCase):
    """C2-37: 15 value cells; C2-38: 14 PATH and 16 NO_PROXY cells; no false refusal; n=5 each."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = {row["cell"]: row for row in cells.run_all(cells.N)}

    def _group(self, *groups: str) -> list[dict]:
        return [row for row in self.rows.values() if row["group"] in groups]

    def _assert_all(self, rows: list[dict], count: int) -> None:
        self.assertEqual(count, len(rows))
        for row in rows:
            with self.subTest(cell=row["cell"]):
                self.assertEqual((cells.N, cells.N), (row["n"], row["pass"]), row)

    def test_fifteen_value_cells_closed(self) -> None:
        self._assert_all(self._group("value"), 15)

    def test_fourteen_path_cells_closed(self) -> None:
        self._assert_all(self._group("path"), 14)

    def test_sixteen_no_proxy_cells_closed(self) -> None:
        self._assert_all(self._group("no_proxy"), 16)

    def test_no_false_refusal(self) -> None:
        self._assert_all(self._group("ordinary", "baseline", "path_ok", "no_proxy_ok"), 3 + 9 + 5 + 6)

    def test_extras_and_c2_40_shapes(self) -> None:
        self._assert_all(self._group("extra", "c2_40"), 8 + 6)

    def test_c1_46_names_carried_and_value_checked(self) -> None:
        self._assert_all(self._group("c1_46"), 7)
        self._assert_all(self._group("c1_46_ok"), 6)

    def test_users_own_claude_sign_in_token_cells(self) -> None:
        self._assert_all(self._group("auth_ok"), 3)

    def test_the_fifteenth_path_cell_is_open_by_default_and_closed_by_the_scan_switch(self) -> None:
        self._assert_all(self._group("path_open", "path_scan"), 2)

    def test_the_hosted_runner_layout_is_refused_by_the_rules_not_met_by_accident(self) -> None:
        # Run 42733662905: /usr/local/bin is 0777 on the hosted image and /usr/bin links into the 0777 /usr/share.
        self._assert_all(self._group("hosted", "hosted_scan"), 3)
        rows = {row["cell"]: row["expect"] for row in self._group("hosted", "hosted_scan")}
        self.assertEqual({"hosted_local_bin_0777": "refused:value_chain_unsafe", "hosted_share_tool": "launched",
                          "hosted_share_tool[PATH_TOOLS=None]": "refused:path_tool_value_file_unsafe"}, rows)

    def test_no_cell_puts_a_host_directory_on_path(self) -> None:
        # Each cell's PATH entries are fixture directories (or /proc, refused by spelling): the host layout decides nothing.
        scratch = cells.Scratch()
        self.addCleanup(scratch.close)
        for cell in cells.cells(scratch):
            for entry in cell.parent.get("PATH", "").split(os.pathsep):
                if entry and os.path.isabs(entry) and not entry.startswith("/proc/"):
                    with self.subTest(cell=cell.name, entry=entry):
                        self.assertEqual(str(scratch.root), os.path.commonpath([str(scratch.root), os.path.normpath(entry)]))


class ProfileTests(unittest.TestCase):
    def test_unknown_or_wrapped_engines_refuse(self) -> None:
        home = _home(self)
        for engine in ("claude", "Codex", "env", "sh", "npx", "codex-cli", "", None, "pi", "hermes", "opencode"):
            with self.subTest(engine=engine), self.assertRaises(EnvRefused) as caught:
                build_child_env(engine, home=home, parent={"PATH": "/usr/bin"})
            self.assertEqual("unknown_engine", caught.exception.cause)

    def test_only_allowlisted_names_reach_either_engine(self) -> None:
        home = _home(self)
        planted = {name: FAKE for name in (
            "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_HOME",
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_OAUTH_TOKEN",
            "ANTHROPIC_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
            "ANTHROPIC_PROFILE", "ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID", "ANTHROPIC_BASE_URL",
            "CLAUDE_CONFIG_DIR", "GEMINI_API_KEY", "XAI_API_KEY", "CURSOR_API_KEY", "GITHUB_TOKEN", "GH_TOKEN",
            "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "NPM_TOKEN", "DATABASE_PASSWORD", "MY_SERVICE_SECRET",
            "SSH_AUTH_SOCK", "LD_PRELOAD", "NODE_OPTIONS", "PYTHONPATH", "CLAUDE_CODE_SHELL", "CLAUDE_ENV_FILE",
            "CODEX_CA_CERTIFICATE", "XDG_CONFIG_HOME", "CLAUDE_SECURESTORAGE_CONFIG_DIR")}
        parent = {"PATH": "/usr/bin:/bin", "HOME": "/home/operator", "LANG": "C.UTF-8", **planted}
        for engine, profile in ENGINE_PROFILES.items():
            with self.subTest(engine=engine):
                env = build_child_env(engine, home=home, parent=parent)
                self.assertLessEqual(set(env), set(profile.allowed) | set(profile.passthrough))
                # The only planted name that may arrive is the user's own sign-in token, on the Claude profile only.
                self.assertEqual(set(profile.passthrough), set(planted) & set(env))
                self.assertEqual({"claude_code": {"CLAUDE_CODE_OAUTH_TOKEN"}, "codex": set()}[engine],
                                 set(profile.passthrough))
                self.assertFalse(set(profile.scrub) & set(env))
                self.assertEqual(home, env["HOME"])
                self.assertEqual(set(profile.passthrough), {k for k, v in env.to_dict().items() if v == FAKE})
                self.assertLessEqual(set(planted) - set(profile.passthrough), set(env.dropped))
                self.assertNotIn(FAKE, repr(env))

    def test_allowlist_counts(self) -> None:
        # 29 Nov 5 names + ALL_PROXY, all_proxy, XDG_RUNTIME_DIR (PRODUCT.md 055cc7a5 section 3.8, C1-46).
        self.assertEqual(32, len(BASE_NAMES))
        self.assertEqual(32, len(set(BASE_NAMES)))
        self.assertLessEqual({"ALL_PROXY", "all_proxy", "XDG_RUNTIME_DIR"}, set(BASE_NAMES))
        self.assertEqual(BASE_NAMES + CLAUDE_TLS_NAMES, ENGINE_PROFILES["claude_code"].allowed)
        self.assertEqual(BASE_NAMES, ENGINE_PROFILES["codex"].allowed)
        self.assertEqual({"CLAUDE_CODE_CLIENT_CERT", "CLAUDE_CODE_CLIENT_KEY", "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE",
                          "CLAUDE_CODE_CERT_STORE"}, set(CLAUDE_TLS_NAMES))

    def test_users_own_oauth_token_passes_to_claude_unchanged(self) -> None:
        """PRODUCT.md 055cc7a5 3.3 (Nov 8 fleet correction): the user's CLAUDE_CODE_OAUTH_TOKEN stays as it is."""

        home = _home(self)
        for value in (FAKE, "", "fake value with spaces/../and=signs"):
            with self.subTest(value=value):
                env = build_child_env("claude_code", home=home, parent={"PATH": "/usr/bin", "CLAUDE_CODE_OAUTH_TOKEN": value})
                self.assertEqual(value, env["CLAUDE_CODE_OAUTH_TOKEN"])
                self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env.dropped)
                self.assertNotIn(FAKE, repr(env))
        # It is not a Codex sign-in: the Codex child never sees it.
        env = build_child_env("codex", home=home, parent={"PATH": "/usr/bin", "CLAUDE_CODE_OAUTH_TOKEN": FAKE})
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", env.dropped)

    def test_oauth_token_absent_in_parent_is_absent_in_child(self) -> None:
        home = _home(self)
        for parent in ({"PATH": "/usr/bin"}, {"PATH": "/usr/bin", "ANTHROPIC_OAUTH_TOKEN": FAKE, "ANTHROPIC_AUTH_TOKEN": FAKE}):
            with self.subTest(parent=sorted(parent)):
                env = build_child_env("claude_code", home=home, parent=parent)
                self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
                self.assertNotIn(FAKE, env.to_dict().values())
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": FAKE}):  # an explicit parent wins over os.environ
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", build_child_env("claude_code", home=home, parent={}))

    def test_oauth_token_and_tls_secrets_cannot_be_set_by_override(self) -> None:
        home = _home(self)
        cases = (("claude_code", "CLAUDE_CODE_OAUTH_TOKEN", {}), ("claude_code", "CLAUDE_CODE_OAUTH_TOKEN", {"CLAUDE_CODE_OAUTH_TOKEN": FAKE}),
                 ("codex", "CLAUDE_CODE_OAUTH_TOKEN", {}), ("claude_code", "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE", {}))
        for engine, name, extra in cases:
            with self.subTest(engine=engine, name=name, parent=sorted(extra)), self.assertRaises(EnvRefused) as caught:
                build_child_env(engine, home=home, parent={"PATH": "/usr/bin", **extra}, overrides={name: "fake-override"})
            self.assertEqual((name, "override_denied"), (caught.exception.name, caught.exception.cause))
        env = build_child_env("claude_code", home=home, parent={"PATH": "/usr/bin", "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE": FAKE})
        self.assertEqual(FAKE, env["CLAUDE_CODE_CLIENT_KEY_PASSPHRASE"])  # the operator's own value is carried

    def test_client_private_key_cannot_be_replaced_by_an_override(self) -> None:
        """Devin r4510867706: CLAUDE_CODE_CLIENT_KEY is a credential path the denylist pattern misses."""

        home = Path(_home(self))
        parent_key = home / "parent-key.pem"
        other_key = home / "other-key.pem"
        for path in (parent_key, other_key):
            path.write_text("not a real key\n", encoding="ascii")
            path.chmod(0o600)
        with self.assertRaises(EnvRefused) as caught:
            build_child_env(
                "claude_code",
                home=str(home),
                parent={"PATH": "/usr/bin", "CLAUDE_CODE_CLIENT_KEY": str(parent_key)},
                overrides={"CLAUDE_CODE_CLIENT_KEY": str(other_key)},
            )
        self.assertEqual(("CLAUDE_CODE_CLIENT_KEY", "override_denied"), (caught.exception.name, caught.exception.cause))
        env = build_child_env(
            "claude_code", home=str(home), parent={"PATH": "/usr/bin", "CLAUDE_CODE_CLIENT_KEY": str(parent_key)}
        )
        self.assertEqual(str(parent_key), env["CLAUDE_CODE_CLIENT_KEY"])
        with self.assertRaises(EnvRefused) as caught:
            build_child_env(
                "codex",
                home=str(home),
                parent={"PATH": "/usr/bin"},
                overrides={"CLAUDE_CODE_CLIENT_KEY": str(other_key)},
            )
        self.assertEqual(("CLAUDE_CODE_CLIENT_KEY", "override_denied"), (caught.exception.name, caught.exception.cause))

    def test_client_certificate_cannot_be_set_by_an_override(self) -> None:
        # Review of 58e7081f: an override could aim CLAUDE_CODE_CLIENT_CERT at the key file or at another cert.
        home = Path(_home(self))
        cert, key, other = home / "cert.pem", home / "key.pem", home / "other.pem"
        for path in (cert, key, other):
            path.write_text("not a real key or certificate\n", encoding="ascii")
            path.chmod(0o600)
        own = {"PATH": "/usr/bin", "CLAUDE_CODE_CLIENT_CERT": str(cert), "CLAUDE_CODE_CLIENT_KEY": str(key)}
        for parent in ({"PATH": "/usr/bin"}, {"PATH": "/usr/bin", "CLAUDE_CODE_CLIENT_KEY": str(key)}, own):
            for target in (key, other):
                with self.subTest(parent=sorted(parent), target=target.name):
                    with self.assertRaises(EnvRefused) as caught:
                        build_child_env("claude_code", home=str(home), parent=parent,
                                        overrides={"CLAUDE_CODE_CLIENT_CERT": str(target)})
                    self.assertEqual(("CLAUDE_CODE_CLIENT_CERT", "override_denied"),
                                     (caught.exception.name, caught.exception.cause))
        # The operator's own certificate and key are still carried, under the file rule.
        env = build_child_env("claude_code", home=str(home), parent=own)
        self.assertEqual((str(cert), str(key)), (env["CLAUDE_CODE_CLIENT_CERT"], env["CLAUDE_CODE_CLIENT_KEY"]))
        with self.assertRaises(EnvRefused) as caught:
            build_child_env("claude_code", home=str(home), parent={"PATH": "/usr/bin", "CLAUDE_CODE_CLIENT_CERT": "cert.pem"})
        self.assertEqual(("CLAUDE_CODE_CLIENT_CERT", "value_relative"), (caught.exception.name, caught.exception.cause))

    def test_claude_code_simple_is_scrubbed_by_name(self) -> None:
        # PRODUCT.md 055cc7a5 section 3.3 names CLAUDE_CODE_SIMPLE in the Claude scrub list (review of 3503f6a6, finding 2).
        home = _home(self)
        self.assertIn("CLAUDE_CODE_SIMPLE", ENGINE_PROFILES["claude_code"].scrub)
        env = build_child_env("claude_code", home=home, parent={"PATH": "/usr/bin", "CLAUDE_CODE_SIMPLE": "1"})
        self.assertNotIn("CLAUDE_CODE_SIMPLE", env)
        self.assertIn("CLAUDE_CODE_SIMPLE", env.dropped)
        with self.assertRaises(EnvRefused) as caught:
            build_child_env("claude_code", home=home, parent={"PATH": "/usr/bin"}, overrides={"CLAUDE_CODE_SIMPLE": "1"})
        self.assertEqual(("CLAUDE_CODE_SIMPLE", "override_denied"), (caught.exception.name, caught.exception.cause))

    def test_codex_scrub_names_are_listed(self) -> None:
        self.assertLessEqual({"CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_API_KEY"}, set(ENGINE_PROFILES["codex"].scrub))
        self.assertLessEqual({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}, set(ENGINE_PROFILES["claude_code"].scrub))
        for profile in ENGINE_PROFILES.values():
            self.assertFalse(set(profile.scrub) & set(profile.allowed), profile.engine)

    def test_engine_hosts_differ_per_profile(self) -> None:
        home = _home(self)
        parent = {"PATH": "/usr/bin", "HTTPS_PROXY": "http://127.0.0.1:3128", "NO_PROXY": ".openai.com"}
        self.assertEqual(".openai.com", build_child_env("claude_code", home=home, parent=parent)["NO_PROXY"])
        with self.assertRaises(EnvRefused) as caught:
            build_child_env("codex", home=home, parent=parent)
        self.assertEqual(("NO_PROXY", "no_proxy_engine_host"), (caught.exception.name, caught.exception.cause))

    def test_overrides_never_carry_credentials(self) -> None:
        home = _home(self)
        for name in ("CODEX_API_KEY", "OPENAI_API_KEY", "MY_TOKEN", "ANTHROPIC_BASE_URL", "CODEX_HOME", "HOME", "PATH"):
            engine = "codex" if name.startswith(("CODEX", "OPENAI")) else "claude_code"
            with self.subTest(name=name), self.assertRaises(EnvRefused) as caught:
                build_child_env(engine, home=home, parent={}, overrides={name: FAKE})
            self.assertEqual("override_denied", caught.exception.cause)

    def test_overrides_cannot_add_names_outside_the_allowlist(self) -> None:
        home = _home(self)
        for engine in ENGINE_PROFILES:
            for name in ("LD_PRELOAD", "NODE_OPTIONS", "PYTHONPATH", "XDG_CONFIG_HOME", "DISABLE_AUTOUPDATER"):
                with self.subTest(engine=engine, name=name), self.assertRaises(EnvRefused) as caught:
                    build_child_env(engine, home=home, parent={"PATH": "/usr/bin"}, overrides={name: "/mock/not-loaded.so"})
                self.assertEqual((name, "override_not_allowed"), (caught.exception.name, caught.exception.cause))

    def test_override_values_meet_the_same_rules_as_inherited_values(self) -> None:
        """Review counterexamples (Nov 7): each override must refuse exactly as the same parent value does."""

        home = _home(self)
        cases = (
            ({"LD_PRELOAD": "/mock/not-loaded.so"}, ("LD_PRELOAD", "override_not_allowed")),
            ({"SSL_CERT_FILE": "relative-cert.pem"}, ("SSL_CERT_FILE", "value_relative")),
            ({"NO_PROXY": "*", "HTTPS_PROXY": "http://proxy.invalid:8080"}, ("NO_PROXY", "no_proxy_wildcard")),
            ({"NO_PROXY": ".openai.com", "HTTPS_PROXY": "http://proxy.invalid:8080"}, ("NO_PROXY", "no_proxy_engine_host")),
            ({"HTTPS_PROXY": "http://u:p@proxy.invalid:8080"}, ("HTTPS_PROXY", "proxy_userinfo")),
            ({"SSL_CERT_DIR": "/proc/self/cwd"}, ("SSL_CERT_DIR", "value_perspective")),
            ({"TZ": "../../etc/zone"}, ("TZ", "tz_relative_path")),
            ({"ALL_PROXY": "socks5://u:p@proxy.invalid:1080"}, ("ALL_PROXY", "proxy_userinfo")),
            ({"NO_PROXY": "*", "ALL_PROXY": "socks5://proxy.invalid:1080"}, ("NO_PROXY", "no_proxy_wildcard")),
            ({"XDG_RUNTIME_DIR": "/proc/self/cwd"}, ("XDG_RUNTIME_DIR", "value_perspective")),
            ({"CLAUDE_CODE_CLIENT_CERT": "relative.pem"}, ("CLAUDE_CODE_CLIENT_CERT", "override_not_allowed")),
        )
        for overrides, expected in cases:
            with self.subTest(overrides=overrides), self.assertRaises(EnvRefused) as caught:
                build_child_env("codex", home=home, parent={"PATH": "/usr/bin"}, overrides=overrides)
            self.assertEqual(expected, (caught.exception.name, caught.exception.cause))
        # Split across parent and override: a proxy override still meets the parent's NO_PROXY, and back.
        for parent, overrides in (({"NO_PROXY": "*"}, {"HTTPS_PROXY": "http://proxy.invalid:8080"}),
                                  ({"HTTPS_PROXY": "http://proxy.invalid:8080"}, {"no_proxy": "*"})):
            with self.subTest(parent=parent), self.assertRaises(EnvRefused) as caught:
                build_child_env("codex", home=home, parent={"PATH": "/usr/bin", **parent}, overrides=overrides)
            self.assertEqual("no_proxy_wildcard", caught.exception.cause)

    def test_safe_overrides_pass(self) -> None:
        home = _home(self)
        cert = Path(home) / "cert.pem"
        cert.write_text("not a real certificate\n", encoding="ascii")
        cert.chmod(0o600)
        overrides = {"LANG": "C.UTF-8", "TZ": "UTC", "SSL_CERT_FILE": str(cert),
                     "HTTPS_PROXY": "http://127.0.0.1:3128", "NO_PROXY": "localhost,127.0.0.1"}
        for engine in ENGINE_PROFILES:
            with self.subTest(engine=engine):
                env = build_child_env(engine, home=home, parent={"PATH": "/usr/bin", "LANG": "C"}, overrides=overrides)
                self.assertEqual(overrides, {name: env[name] for name in overrides})
                self.assertEqual(home, env["HOME"])
        # A temp override is replaced by the private route temp root, exactly as a parent TMPDIR is.
        env = build_child_env("codex", home=home, parent={"PATH": "/usr/bin"}, overrides={"TMPDIR": "/tmp"})
        self.assertNotEqual("/tmp", env["TMPDIR"])
        self.assertEqual(0, os.lstat(env["TMPDIR"]).st_mode & 0o077)

    def test_home_must_be_private(self) -> None:
        home = _home(self)
        os.chmod(home, 0o755)
        for value in (home, "relative/home", "/nonexistent-csl785", None):
            with self.subTest(home=value), self.assertRaises(EnvRefused) as caught:
                build_child_env("codex", home=value, parent={})
            self.assertEqual(("HOME", "home_unsafe"), (caught.exception.name, caught.exception.cause))

    def test_home_behind_a_link_whose_resolved_parent_others_can_write_is_refused(self) -> None:
        # #1098 review F4: the link itself is ours and safe; the directory it resolves into is not.
        base = Path(_home(self))
        for mode in (0o777, 0o775, 0o757, 0o1777):  # 1777 is ours, not root's, so the sticky exception does not apply
            with self.subTest(mode=oct(mode)):
                if mode == 0o1777 and _running_as_root():
                    # As root the directory is root-owned, so the root-owned sticky exception applies.
                    self.skipTest("mode 1777 is only 'ours, not root's' when euid is not 0")
                real_parent = base / f"real-{mode:o}"
                real_parent.mkdir(mode=0o700)
                (real_parent / "home").mkdir(mode=0o700)
                real_parent.chmod(mode)
                link = base / f"link-{mode:o}"
                os.symlink(real_parent, link)
                with self.assertRaises(EnvRefused) as caught:
                    build_child_env("codex", home=str(link / "home"), parent={})
                self.assertEqual(("HOME", "home_unsafe"), (caught.exception.name, caught.exception.cause))
                real_parent.chmod(0o700)  # control: the same link and home pass once the resolved parent is private
                self.assertEqual(str(link / "home"), build_child_env("codex", home=str(link / "home"), parent={})["HOME"])
        # A link two levels up whose target's parent others can write is refused too.
        loose = base / "loose"
        loose.mkdir(mode=0o700)
        (loose / "mid").mkdir(mode=0o700)
        (loose / "mid" / "home").mkdir(mode=0o700)
        loose.chmod(0o777)
        os.symlink(loose / "mid", base / "deep-link")
        with self.assertRaises(EnvRefused) as caught:
            build_child_env("codex", home=str(base / "deep-link" / "home"), parent={})
        self.assertEqual("home_unsafe", caught.exception.cause)

    def test_home_under_a_symlinked_parent_is_accepted(self) -> None:
        # Devin r4503135764: macOS /var -> private/var. Modelled with an own-uid link, which _resolve also accepts.
        base = Path(_home(self))
        real_parent = base / "real"
        real_parent.mkdir(mode=0o700)
        (real_parent / "home").mkdir(mode=0o700)
        os.symlink(real_parent, base / "link")
        env = build_child_env("codex", home=str(base / "link" / "home"), parent={})
        self.assertEqual(str(base / "link" / "home"), env["HOME"])
        os.symlink(real_parent / "home", base / "home-link")
        with self.assertRaises(EnvRefused) as caught:  # the home itself being a link stays refused
            build_child_env("codex", home=str(base / "home-link"), parent={})
        self.assertEqual("home_unsafe", caught.exception.cause)

    def test_a_linked_home_stays_refused_with_a_trailing_slash_or_dot(self) -> None:
        """Devin r4508023438: lstat follows link/ and link/., so both spellings must refuse."""

        base = Path(_home(self))
        real = base / "real-home"
        real.mkdir(mode=0o700)
        link = base / "home-link"
        os.symlink(real, link)
        for spelling in (str(link), str(link) + "/", str(link) + "/.", str(link) + "/./", str(link) + "//"):
            with self.subTest(home=spelling), self.assertRaises(EnvRefused) as caught:
                build_child_env("codex", home=spelling, parent={})
            self.assertEqual(("HOME", "home_unsafe"), (caught.exception.name, caught.exception.cause))
        parent_link = base / "parent-link"
        os.symlink(base, parent_link)
        accepted = (
            str(real),
            str(real) + "/",
            str(real) + "/.",
            str(parent_link / "real-home"),
            str(parent_link / "real-home") + "/",
            str(parent_link / "real-home") + "/.",
        )
        for spelling in accepted:
            with self.subTest(home=spelling):
                self.assertEqual(spelling, build_child_env("codex", home=spelling, parent={})["HOME"])

    def test_a_linked_home_is_refused_when_spelled_through_dot_dot(self) -> None:
        # Review of 58e7081f: link/sub/.. reaches the link's target, and lstat of it sees a directory.
        base = Path(_home(self))
        real_home = base / "real-home"
        real_home.mkdir(mode=0o700)
        (real_home / "sub").mkdir(mode=0o700)
        os.symlink(real_home, base / "home-link")
        link = str(base / "home-link")
        # The last spelling has no link in it: a home ending in ".." is refused whatever it reaches.
        for home in (link + "/sub/..", link + "/sub/../", link + "/sub/../.", link + "/./sub/..", link + "//sub/..",
                     f"{base}/real-home/../home-link/", f"{real_home}/sub/.."):
            with self.subTest(home=home[len(str(base)):]), self.assertRaises(EnvRefused) as caught:
                build_child_env("codex", home=home, parent={})
            self.assertEqual(("HOME", "home_unsafe"), (caught.exception.name, caught.exception.cause))
        # Control: ".." before the last named entry is fine, and the child gets HOME as it was given.
        for home in (f"{base}/home-link/../real-home", f"{real_home}/sub/../../real-home/", f"{link}/sub/../sub",
                     f"{link}/sub/"):
            with self.subTest(home=home[len(str(base)):]):
                self.assertEqual(home, build_child_env("codex", home=home, parent={})["HOME"])

    def test_validated_env_cannot_be_hand_made(self) -> None:
        with self.assertRaises(TypeError):
            ValidatedEnv({"PATH": "/usr/bin"}, "codex", ())

    def test_windows_refuses_instead_of_passing_values_unchecked(self) -> None:
        with mock.patch("recursion_core.child_env.policy.os.name", "nt"), self.assertRaises(EnvRefused) as caught:
            build_child_env("codex", home="C:\\x", parent={})
        self.assertEqual("platform_unsupported", caught.exception.cause)


@unittest.skipIf(os.name == "nt", "POSIX rules")
class LaunchTests(unittest.TestCase):
    """The boundary passes exactly the validated env, never the parent's, and refuses before Popen."""

    def setUp(self) -> None:
        self.home = _home(self)
        self.bin = Path(_home(self))
        self.engine_bin = self.bin / "fake-engine"
        self.engine_bin.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        self.engine_bin.chmod(0o755)
        self.popen = mock.MagicMock(name="Popen")

    def _env(self, engine: str = "codex", **extra: str) -> ValidatedEnv:
        parent = {"PATH": f"{self.bin}:/usr/bin:/bin", "LANG": "C.UTF-8", **extra}
        return build_child_env(engine, home=self.home, parent=parent)

    def test_popen_gets_exactly_the_validated_env_not_the_parent(self) -> None:
        planted = {"CODEX_API_KEY": FAKE, "CODEX_ACCESS_TOKEN": FAKE, "OPENAI_API_KEY": FAKE, "ANTHROPIC_API_KEY": FAKE}
        with mock.patch.dict(os.environ, planted):
            env = self._env(**planted)
            spawn(["fake-engine", "exec", "--json"], env, cwd=self.home, popen=self.popen)
        self.popen.assert_called_once()
        (argv,), kwargs = self.popen.call_args
        self.assertEqual([str(self.engine_bin), "exec", "--json"], argv)
        self.assertIsNot(kwargs["env"], os.environ)
        self.assertEqual(env.to_dict(), kwargs["env"])
        self.assertFalse(set(planted) & set(kwargs["env"]))
        self.assertEqual((False, True, True), (kwargs["shell"], kwargs["close_fds"], kwargs["start_new_session"]))

    def test_a_plain_mapping_or_os_environ_is_refused(self) -> None:
        for env in (dict(os.environ), os.environ, {"PATH": "/usr/bin"}, None):
            with self.subTest(env=type(env).__name__), self.assertRaises(TypeError):
                spawn(["fake-engine"], env, popen=self.popen)
        self.popen.assert_not_called()

    def test_caller_cannot_replace_env_or_widen_the_launch(self) -> None:
        env = self._env()
        for key in ("env", "shell", "executable", "preexec_fn", "pass_fds", "close_fds"):
            with self.subTest(key=key), self.assertRaises((EnvRefused, TypeError)):  # env= is a TypeError
                spawn(["fake-engine"], env, popen=self.popen, **{key: None})
        self.popen.assert_not_called()

    def test_caller_cannot_put_the_child_in_the_parents_session(self) -> None:
        # Devin r4503135702: start_new_session=False or a process_group would undo the child's own session.
        env = self._env()
        for kwargs in ({"start_new_session": False}, {"start_new_session": None}, {"process_group": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(EnvRefused) as caught:
                spawn(["fake-engine"], env, popen=self.popen, **kwargs)
            self.assertTrue(caught.exception.cause.startswith("launch_option_denied:"))
        self.popen.assert_not_called()
        spawn(["fake-engine"], env, popen=self.popen, start_new_session=True)
        self.assertIs(True, self.popen.call_args.kwargs["start_new_session"])

    def test_argv0_resolves_only_on_the_validated_path(self) -> None:
        env = build_child_env("codex", home=self.home, parent={"PATH": "/usr/bin:/bin"})
        with mock.patch.dict(os.environ, {"PATH": f"{self.bin}:/usr/bin"}), self.assertRaises(EnvRefused) as caught:
            spawn(["fake-engine"], env, popen=self.popen)
        self.assertEqual("argv0_not_found", caught.exception.cause)
        for argv0, cause in (("./fake-engine", "argv0_relative"), ("bin/fake-engine", "argv0_relative")):
            with self.subTest(argv0=argv0), self.assertRaises(EnvRefused) as caught:
                spawn([argv0], env, popen=self.popen)
            self.assertEqual(cause, caught.exception.cause)
        self.popen.assert_not_called()

    def test_an_engine_binary_someone_else_can_replace_is_refused(self) -> None:
        loose = Path(_home(self))
        (loose / "fake-engine").write_text("#!/bin/sh\n", encoding="ascii")
        (loose / "fake-engine").chmod(0o755)
        loose.chmod(0o777)
        env = self._env()
        with self.assertRaises(EnvRefused) as caught:
            spawn([str(loose / "fake-engine")], env, popen=self.popen)
        self.assertEqual("argv0_value_chain_unsafe", caught.exception.cause)
        self.popen.assert_not_called()

    def test_a_refused_environment_never_reaches_popen(self) -> None:
        with self.assertRaises(EnvRefused):
            env = self._env(HTTPS_PROXY="http://u:p@127.0.0.1:9")
            spawn(["fake-engine"], env, popen=self.popen)
        self.popen.assert_not_called()

    def test_a_bad_override_never_reaches_popen(self) -> None:
        overrides = ({"LD_PRELOAD": "/mock/not-loaded.so"}, {"SSL_CERT_FILE": "relative-cert.pem"},
                     {"NO_PROXY": "*", "HTTPS_PROXY": "http://proxy.invalid:8080"})
        parent = {"PATH": f"{self.bin}:/usr/bin:/bin"}  # fake-engine resolves, so only the override can refuse
        for bad in overrides:
            for _ in range(5):
                with self.subTest(overrides=bad), self.assertRaises(EnvRefused):
                    env = build_child_env("codex", home=self.home, parent=parent, overrides=bad)
                    spawn(["fake-engine"], env, popen=self.popen)
        self.popen.assert_not_called()
        env = build_child_env("codex", home=self.home, parent=parent, overrides={"LANG": "C.UTF-8"})
        spawn(["fake-engine"], env, popen=self.popen)
        self.assertEqual("C.UTF-8", self.popen.call_args.kwargs["env"]["LANG"])

    def test_real_child_sees_only_the_validated_env(self) -> None:
        """One real local child (/usr/bin/env, no engine): it prints exactly the validated names."""

        if not os.path.exists("/usr/bin/env"):
            self.skipTest("needs /usr/bin/env")
        with mock.patch.dict(os.environ, {"CODEX_API_KEY": FAKE, "OPENAI_API_KEY": FAKE}):
            env = build_child_env("codex", home=self.home, parent={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                                                                  "CODEX_API_KEY": FAKE})
            proc = spawn(["/usr/bin/env"], env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, _ = proc.communicate(timeout=10)
        names = {line.split("=", 1)[0] for line in out.decode().splitlines() if "=" in line}
        self.assertEqual(set(env), names)
        self.assertNotIn(FAKE, out.decode())

    def _fresh_tmp(self) -> None:
        from recursion_core.child_env import policy
        saved = dict(policy._TMP)
        policy._TMP.clear()
        self.addCleanup(lambda: (policy._TMP.clear(), policy._TMP.update(saved)))

    def test_route_temp_root_never_sits_in_a_directory_others_can_write(self) -> None:
        # Devin r4503135904: the daemon's own TMPDIR may point at another user's writable directory.
        self._fresh_tmp()
        unsafe = _home(self)
        os.chmod(unsafe, 0o777)  # writable by others, not sticky: anyone could rename what we make there
        with mock.patch("recursion_core.child_env.policy.tempfile.gettempdir", return_value=unsafe):
            env = self._env(TMPDIR="/tmp")
        self.assertFalse(env["TMPDIR"].startswith(unsafe + os.sep))
        self.assertEqual([], os.listdir(unsafe))  # the rejected attempt was removed
        st = os.lstat(env["TMPDIR"])
        self.assertEqual((os.geteuid(), 0), (st.st_uid, st.st_mode & 0o077))

    def test_route_temp_root_refuses_when_no_safe_base_exists(self) -> None:
        self._fresh_tmp()
        unsafe = _home(self)
        os.chmod(unsafe, 0o777)
        with mock.patch("recursion_core.child_env.policy._tmp_bases", return_value=[unsafe]), \
                self.assertRaises(EnvRefused) as caught:
            self._env(TMPDIR="/tmp")
        self.assertEqual(("TMPDIR", "tmp_root_unsafe"), (caught.exception.name, caught.exception.cause))
        self.assertEqual([], os.listdir(unsafe))

    def test_route_temp_root_is_private(self) -> None:
        env = self._env(TMPDIR="/tmp")
        st = os.lstat(env["TMPDIR"])
        self.assertTrue(stat.S_ISDIR(st.st_mode))
        self.assertEqual((os.geteuid(), 0), (st.st_uid, st.st_mode & 0o077))


if __name__ == "__main__":
    unittest.main(verbosity=2)
