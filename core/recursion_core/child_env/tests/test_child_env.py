"""CSL-785: child-environment allowlist and launch boundary in the new core (candidate tests).

Run from the repository root: ``PYTHONPATH=core python3 -B -m unittest discover -s core/recursion_core/child_env/tests -t core -v``.
No engine, provider, network or real credential is used; every secret-shaped value is fake.
"""

from __future__ import annotations

import ast
import os
import shutil
import stat
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from recursion_core.child_env import (
    AUTH_MODES,
    BASE_NAMES,
    ENGINE_PROFILES,
    ChildRoute,
    EnvRefused,
    ValidatedEnv,
    build_child_env,
    launch,
    policy,
    spawn,
)
from recursion_core.child_env.policy import CLAUDE_TLS_NAMES
from recursion_core.child_env.tests import cells


def _running_as_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


FAKE = "fake-value-not-a-credential"
SUB, API, CLOUD = "subscription", "api_key", "cloud_provider"
USER_KEY = "fake-user-entered-key-not-a-credential"  # stands for a key the caller already fetched
KEY, URL, OAUTH = "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN"
BEDROCK, VERTEX, FOUNDRY = "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"
# A minimal valid route per mode, as a caller would pass it.
ROUTES = {SUB: ChildRoute(), API: ChildRoute(api_key=USER_KEY), CLOUD: ChildRoute(provider_switches=(BEDROCK,))}


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

    def test_auth_mode_and_route_cells(self) -> None:
        self._assert_all(self._group("auth_mode"), 5)
        self._assert_all(self._group("auth_route"), 14)
        self._assert_all(self._group("auth_route_ok"), 10)

    def test_subscription_base_url_cells(self) -> None:
        self._assert_all(self._group("sub_base_url"), 12)
        self._assert_all(self._group("sub_base_url_ok"), 9)

    def test_a_bracketed_host_must_be_an_ipv6_address_cells(self) -> None:
        self._assert_all(self._group("bracket_host"), 9)
        self._assert_all(self._group("bracket_host_ok"), 5)

    def test_a_dot_dot_out_of_a_directory_others_can_write_is_refused(self) -> None:
        self._assert_all(self._group("dotdot"), 5)
        self._assert_all(self._group("dotdot_ok"), 4)

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
                build_child_env(engine, home=home, auth_mode=SUB, parent={"PATH": "/usr/bin"})
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
                env = build_child_env(engine, home=home, auth_mode=SUB, parent=parent)
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
                env = build_child_env("claude_code", home=home, auth_mode=SUB, parent={"PATH": "/usr/bin", "CLAUDE_CODE_OAUTH_TOKEN": value})
                self.assertEqual(value, env["CLAUDE_CODE_OAUTH_TOKEN"])
                self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env.dropped)
                self.assertNotIn(FAKE, repr(env))
        # It is not a Codex sign-in: the Codex child never sees it.
        env = build_child_env("codex", home=home, auth_mode=SUB, parent={"PATH": "/usr/bin", "CLAUDE_CODE_OAUTH_TOKEN": FAKE})
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", env.dropped)

    def test_oauth_token_absent_in_parent_is_absent_in_child(self) -> None:
        home = _home(self)
        for parent in ({"PATH": "/usr/bin"}, {"PATH": "/usr/bin", "ANTHROPIC_OAUTH_TOKEN": FAKE, "ANTHROPIC_AUTH_TOKEN": FAKE}):
            with self.subTest(parent=sorted(parent)):
                env = build_child_env("claude_code", home=home, auth_mode=SUB, parent=parent)
                self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
                self.assertNotIn(FAKE, env.to_dict().values())
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": FAKE}):  # an explicit parent wins over os.environ
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", build_child_env("claude_code", home=home, auth_mode=SUB, parent={}))

    def test_oauth_token_and_tls_secrets_cannot_be_set_by_override(self) -> None:
        home = _home(self)
        cases = (("claude_code", "CLAUDE_CODE_OAUTH_TOKEN", {}), ("claude_code", "CLAUDE_CODE_OAUTH_TOKEN", {"CLAUDE_CODE_OAUTH_TOKEN": FAKE}),
                 ("codex", "CLAUDE_CODE_OAUTH_TOKEN", {}), ("claude_code", "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE", {}))
        for engine, name, extra in cases:
            with self.subTest(engine=engine, name=name, parent=sorted(extra)), self.assertRaises(EnvRefused) as caught:
                build_child_env(engine, home=home, auth_mode=SUB, parent={"PATH": "/usr/bin", **extra}, overrides={name: "fake-override"})
            self.assertEqual((name, "override_denied"), (caught.exception.name, caught.exception.cause))
        env = build_child_env("claude_code", home=home, auth_mode=SUB, parent={"PATH": "/usr/bin", "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE": FAKE})
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
                auth_mode=SUB,
                parent={"PATH": "/usr/bin", "CLAUDE_CODE_CLIENT_KEY": str(parent_key)},
                overrides={"CLAUDE_CODE_CLIENT_KEY": str(other_key)},
            )
        self.assertEqual(("CLAUDE_CODE_CLIENT_KEY", "override_denied"), (caught.exception.name, caught.exception.cause))
        env = build_child_env(
            "claude_code", home=str(home), auth_mode=SUB, parent={"PATH": "/usr/bin", "CLAUDE_CODE_CLIENT_KEY": str(parent_key)}
        )
        self.assertEqual(str(parent_key), env["CLAUDE_CODE_CLIENT_KEY"])
        with self.assertRaises(EnvRefused) as caught:
            build_child_env(
                "codex",
                home=str(home),
                auth_mode=SUB,
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
                        build_child_env("claude_code", home=str(home), auth_mode=SUB, parent=parent,
                                        overrides={"CLAUDE_CODE_CLIENT_CERT": str(target)})
                    self.assertEqual(("CLAUDE_CODE_CLIENT_CERT", "override_denied"),
                                     (caught.exception.name, caught.exception.cause))
        # The operator's own certificate and key are still carried, under the file rule.
        env = build_child_env("claude_code", home=str(home), auth_mode=SUB, parent=own)
        self.assertEqual((str(cert), str(key)), (env["CLAUDE_CODE_CLIENT_CERT"], env["CLAUDE_CODE_CLIENT_KEY"]))
        with self.assertRaises(EnvRefused) as caught:
            build_child_env("claude_code", home=str(home), auth_mode=SUB, parent={"PATH": "/usr/bin", "CLAUDE_CODE_CLIENT_CERT": "cert.pem"})
        self.assertEqual(("CLAUDE_CODE_CLIENT_CERT", "value_relative"), (caught.exception.name, caught.exception.cause))

    def test_claude_code_simple_is_scrubbed_by_name(self) -> None:
        # PRODUCT.md 055cc7a5 section 3.3 names CLAUDE_CODE_SIMPLE in the Claude scrub list (review of 3503f6a6, finding 2).
        home = _home(self)
        self.assertIn("CLAUDE_CODE_SIMPLE", ENGINE_PROFILES["claude_code"].scrub)
        env = build_child_env("claude_code", home=home, auth_mode=SUB, parent={"PATH": "/usr/bin", "CLAUDE_CODE_SIMPLE": "1"})
        self.assertNotIn("CLAUDE_CODE_SIMPLE", env)
        self.assertIn("CLAUDE_CODE_SIMPLE", env.dropped)
        with self.assertRaises(EnvRefused) as caught:
            build_child_env("claude_code", home=home, auth_mode=SUB, parent={"PATH": "/usr/bin"}, overrides={"CLAUDE_CODE_SIMPLE": "1"})
        self.assertEqual(("CLAUDE_CODE_SIMPLE", "override_denied"), (caught.exception.name, caught.exception.cause))

    def test_codex_scrub_names_are_listed(self) -> None:
        self.assertLessEqual({"CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_API_KEY"}, set(ENGINE_PROFILES["codex"].scrub))
        self.assertLessEqual({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}, set(ENGINE_PROFILES["claude_code"].scrub))
        for profile in ENGINE_PROFILES.values():
            self.assertFalse(set(profile.scrub) & set(profile.allowed), profile.engine)

    def test_engine_hosts_differ_per_profile(self) -> None:
        home = _home(self)
        parent = {"PATH": "/usr/bin", "HTTPS_PROXY": "http://127.0.0.1:3128", "NO_PROXY": ".openai.com"}
        self.assertEqual(".openai.com", build_child_env("claude_code", home=home, auth_mode=SUB, parent=parent)["NO_PROXY"])
        with self.assertRaises(EnvRefused) as caught:
            build_child_env("codex", home=home, auth_mode=SUB, parent=parent)
        self.assertEqual(("NO_PROXY", "no_proxy_engine_host"), (caught.exception.name, caught.exception.cause))

    def test_overrides_never_carry_credentials(self) -> None:
        home = _home(self)
        for name in ("CODEX_API_KEY", "OPENAI_API_KEY", "MY_TOKEN", "ANTHROPIC_BASE_URL", "CODEX_HOME", "HOME", "PATH"):
            engine = "codex" if name.startswith(("CODEX", "OPENAI")) else "claude_code"
            with self.subTest(name=name), self.assertRaises(EnvRefused) as caught:
                build_child_env(engine, home=home, auth_mode=SUB, parent={}, overrides={name: FAKE})
            self.assertEqual("override_denied", caught.exception.cause)

    def test_overrides_cannot_add_names_outside_the_allowlist(self) -> None:
        home = _home(self)
        for engine in ENGINE_PROFILES:
            for name in ("LD_PRELOAD", "NODE_OPTIONS", "PYTHONPATH", "XDG_CONFIG_HOME", "DISABLE_AUTOUPDATER"):
                with self.subTest(engine=engine, name=name), self.assertRaises(EnvRefused) as caught:
                    build_child_env(engine, home=home, auth_mode=SUB, parent={"PATH": "/usr/bin"}, overrides={name: "/mock/not-loaded.so"})
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
                build_child_env("codex", home=home, auth_mode=SUB, parent={"PATH": "/usr/bin"}, overrides=overrides)
            self.assertEqual(expected, (caught.exception.name, caught.exception.cause))
        # Split across parent and override: a proxy override still meets the parent's NO_PROXY, and back.
        for parent, overrides in (({"NO_PROXY": "*"}, {"HTTPS_PROXY": "http://proxy.invalid:8080"}),
                                  ({"HTTPS_PROXY": "http://proxy.invalid:8080"}, {"no_proxy": "*"})):
            with self.subTest(parent=parent), self.assertRaises(EnvRefused) as caught:
                build_child_env("codex", home=home, auth_mode=SUB, parent={"PATH": "/usr/bin", **parent}, overrides=overrides)
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
                env = build_child_env(engine, home=home, auth_mode=SUB, parent={"PATH": "/usr/bin", "LANG": "C"}, overrides=overrides)
                self.assertEqual(overrides, {name: env[name] for name in overrides})
                self.assertEqual(home, env["HOME"])
        # A temp override is replaced by the private route temp root, exactly as a parent TMPDIR is.
        env = build_child_env("codex", home=home, auth_mode=SUB, parent={"PATH": "/usr/bin"}, overrides={"TMPDIR": "/tmp"})
        self.assertNotEqual("/tmp", env["TMPDIR"])
        self.assertEqual(0, os.lstat(env["TMPDIR"]).st_mode & 0o077)

    def test_home_must_be_private(self) -> None:
        home = _home(self)
        os.chmod(home, 0o755)
        for value in (home, "relative/home", "/nonexistent-csl785", None):
            with self.subTest(home=value), self.assertRaises(EnvRefused) as caught:
                build_child_env("codex", home=value, auth_mode=SUB, parent={})
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
                    build_child_env("codex", home=str(link / "home"), auth_mode=SUB, parent={})
                self.assertEqual(("HOME", "home_unsafe"), (caught.exception.name, caught.exception.cause))
                real_parent.chmod(0o700)  # control: the same link and home pass once the resolved parent is private
                self.assertEqual(str(link / "home"), build_child_env("codex", home=str(link / "home"), auth_mode=SUB, parent={})["HOME"])
        # A link two levels up whose target's parent others can write is refused too.
        loose = base / "loose"
        loose.mkdir(mode=0o700)
        (loose / "mid").mkdir(mode=0o700)
        (loose / "mid" / "home").mkdir(mode=0o700)
        loose.chmod(0o777)
        os.symlink(loose / "mid", base / "deep-link")
        with self.assertRaises(EnvRefused) as caught:
            build_child_env("codex", home=str(base / "deep-link" / "home"), auth_mode=SUB, parent={})
        self.assertEqual("home_unsafe", caught.exception.cause)

    def test_home_under_a_symlinked_parent_is_accepted(self) -> None:
        # Devin r4503135764: macOS /var -> private/var. Modelled with an own-uid link, which _resolve also accepts.
        base = Path(_home(self))
        real_parent = base / "real"
        real_parent.mkdir(mode=0o700)
        (real_parent / "home").mkdir(mode=0o700)
        os.symlink(real_parent, base / "link")
        env = build_child_env("codex", home=str(base / "link" / "home"), auth_mode=SUB, parent={})
        self.assertEqual(str(base / "link" / "home"), env["HOME"])
        os.symlink(real_parent / "home", base / "home-link")
        with self.assertRaises(EnvRefused) as caught:  # the home itself being a link stays refused
            build_child_env("codex", home=str(base / "home-link"), auth_mode=SUB, parent={})
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
                build_child_env("codex", home=spelling, auth_mode=SUB, parent={})
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
                self.assertEqual(spelling, build_child_env("codex", home=spelling, auth_mode=SUB, parent={})["HOME"])

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
                build_child_env("codex", home=home, auth_mode=SUB, parent={})
            self.assertEqual(("HOME", "home_unsafe"), (caught.exception.name, caught.exception.cause))
        # Control: ".." before the last named entry is fine, and the child gets HOME as it was given.
        for home in (f"{base}/home-link/../real-home", f"{real_home}/sub/../../real-home/", f"{link}/sub/../sub",
                     f"{link}/sub/"):
            with self.subTest(home=home[len(str(base)):]):
                self.assertEqual(home, build_child_env("codex", home=home, auth_mode=SUB, parent={})["HOME"])

    @unittest.skipIf(os.name == "nt", "POSIX rules")
    def test_a_directory_left_by_dot_dot_is_checked_before_it_is_dropped(self) -> None:
        # #320 review: hop is a plain directory, so no link rule fires, and ".." used to drop it unchecked.
        root = Path(_home(self))
        safe, pub = root / "safe", root / "safe" / "pub"
        (pub / "hop").mkdir(parents=True)
        (safe / "certs").mkdir()
        (safe / "ca.pem").write_text("fake\n", encoding="ascii")
        (safe / "ca.pem").chmod(0o644)
        for path, mode in ((safe, 0o755), (safe / "certs", 0o755), (pub / "hop", 0o755), (pub, 0o777)):
            path.chmod(mode)
        self.assertIsNone(policy._unsafe_file(f"{safe}/ca.pem"))
        self.assertEqual("value_missing", policy._unsafe_file(f"{pub}/x"))
        self.assertEqual("value_chain_unsafe", policy._unsafe_file(f"{pub}/hop/../../ca.pem"))
        self.assertEqual("value_chain_unsafe", policy._unsafe_dir(f"{pub}/hop/../../certs"))
        self.assertEqual("value_chain_unsafe", policy._unsafe_dir(f"{pub}/hop/.."))
        self.assertEqual("value_chain_unsafe", policy._unsafe_path(f"{pub}/hop/../../certs"))
        os.symlink("pub/hop/../../ca.pem", safe / "via")  # the same ".." met in a link's target
        self.assertEqual("value_chain_unsafe", policy._unsafe_file(f"{safe}/via"))
        # Control: ".." out of directories only we can write passes, in a value and in a link's target.
        self.assertIsNone(policy._unsafe_file(f"{safe}/certs/../ca.pem"))
        self.assertIsNone(policy._unsafe_file(f"{safe}/certs/../certs/../ca.pem"))
        self.assertIsNone(policy._unsafe_dir(f"{safe}/certs/../certs"))
        self.assertIsNone(policy._unsafe_path(f"{safe}/certs/../certs"))
        os.symlink("certs/../ca.pem", safe / "owned-via")
        self.assertIsNone(policy._unsafe_file(f"{safe}/owned-via"))

    def test_validated_env_cannot_be_hand_made(self) -> None:
        with self.assertRaises(TypeError):
            ValidatedEnv({"PATH": "/usr/bin"}, "codex", ())

    def test_windows_refuses_instead_of_passing_values_unchecked(self) -> None:
        with mock.patch("recursion_core.child_env.policy.os.name", "nt"), self.assertRaises(EnvRefused) as caught:
            build_child_env("codex", home="C:\\x", auth_mode=SUB, parent={})
        self.assertEqual("platform_unsupported", caught.exception.cause)


@unittest.skipIf(os.name == "nt", "POSIX rules")
class AuthRouteTests(unittest.TestCase):
    """CSLR-D-011, PRODUCT.md 055cc7a5 section 3.3: an explicit auth mode, and a route-owned key, base URL and switches."""

    AMBIENT = {KEY: FAKE, "ANTHROPIC_AUTH_TOKEN": FAKE, URL: "https://ambient.invalid", BEDROCK: "1", VERTEX: "1",
               FOUNDRY: "1", "CODEX_API_KEY": FAKE, "OPENAI_API_KEY": FAKE, "OPENAI_BASE_URL": "https://ambient.invalid"}

    def _refused(self, engine: str, home: str, **kwargs: object) -> tuple[str | None, str]:
        with self.assertRaises(EnvRefused) as caught:
            build_child_env(engine, home=home, **kwargs)
        return caught.exception.name, caught.exception.cause

    def test_auth_mode_is_required_and_one_of_exactly_three(self) -> None:
        home = _home(self)
        self.assertEqual(("subscription", "api_key", "cloud_provider"), AUTH_MODES)
        for engine in ENGINE_PROFILES:
            with self.subTest(engine=engine):
                self.assertEqual((None, "auth_mode_missing"), self._refused(engine, home, parent={"PATH": "/usr/bin"}))
                self.assertEqual((None, "auth_mode_missing"), self._refused(engine, home, auth_mode=None, parent={}))
                # A complete route does not stand in for the mode.
                self.assertEqual((None, "auth_mode_missing"), self._refused(engine, home, route=ROUTES[API], parent={}))
            for mode in ("", "oauth", "Subscription", "API_KEY", "api-key", "bedrock", "cloud", " subscription", 1, True,
                         b"api_key", ("api_key",), ["subscription"]):
                with self.subTest(engine=engine, mode=mode):
                    self.assertEqual((None, "auth_mode_unknown"),
                                     self._refused(engine, home, auth_mode=mode, route=ROUTES[API], parent={}))
        for mode in AUTH_MODES:
            with self.subTest(mode=mode):
                env = build_child_env("claude_code", home=home, auth_mode=mode, route=ROUTES[mode], parent={"PATH": "/usr/bin"})
                self.assertEqual(mode, env.auth_mode)
                self.assertIn(repr(mode), repr(env))

    def test_the_mode_is_never_taken_from_the_launching_shell(self) -> None:
        # A parent that looks like an API-key or a Bedrock shell does not pick the mode, and no mode is the default.
        home = _home(self)
        for parent in ({KEY: FAKE}, {BEDROCK: "1"}, {OAUTH: FAKE}, {"RECURSION_AUTH_MODE": "api_key", "AUTH_MODE": "api_key"}):
            with self.subTest(parent=sorted(parent)):
                self.assertEqual((None, "auth_mode_missing"), self._refused("claude_code", home, parent=parent))
                with mock.patch.dict(os.environ, parent):
                    self.assertEqual((None, "auth_mode_missing"), self._refused("claude_code", home))

    def test_ambient_base_url_is_dropped_in_every_mode(self) -> None:
        home = _home(self)
        for mode in AUTH_MODES:
            for ambient in ("https://ambient.invalid", "http://127.0.0.1:9", "", "not a url"):
                with self.subTest(mode=mode, ambient=ambient):
                    parent = {"PATH": "/usr/bin", URL: ambient, "OPENAI_BASE_URL": ambient}
                    env = build_child_env("claude_code", home=home, auth_mode=mode, route=ROUTES[mode], parent=parent)
                    self.assertNotIn(URL, env)
                    self.assertIn(URL, env.dropped)
                    # os.environ as the parent: the same. Cleared first, so the host's own PATH decides nothing.
                    with mock.patch.dict(os.environ, {"PATH": "/usr/bin", URL: ambient}, clear=True):
                        self.assertNotIn(URL, build_child_env("claude_code", home=home, auth_mode=mode, route=ROUTES[mode]))

    def test_route_base_url_is_set_only_from_the_route_and_a_subscription_route_needs_trust(self) -> None:
        home = _home(self)
        for mode in AUTH_MODES:
            for url in ("https://gateway.invalid", "https://gateway.invalid/api", "http://127.0.0.1:4000",
                        "https://[::1]:8443/v1/", "HTTPS://Gateway.invalid:443/anthropic"):
                with self.subTest(mode=mode, url=url):
                    route = ChildRoute(api_key=ROUTES[mode].api_key, base_url=url,
                                       provider_switches=ROUTES[mode].provider_switches)
                    parent = {"PATH": "/usr/bin", URL: "https://ambient.invalid"}
                    if mode == SUB:  # the sign-in token would go there: see SubscriptionBaseUrlTests
                        self.assertEqual((URL, "route_base_url_untrusted"),
                                         self._refused("claude_code", home, auth_mode=mode, route=route, parent=parent))
                        continue
                    env = build_child_env("claude_code", home=home, auth_mode=mode, route=route, parent=parent)
                    self.assertEqual(url, env[URL])

    def test_route_base_url_meets_the_url_rule(self) -> None:
        home = _home(self)
        bad = (("https://fakeuser:fakepass@gateway.invalid", "base_url_userinfo"), ("https://fakeuser@gateway.invalid/api", "base_url_userinfo"),
               ("gateway.invalid", "base_url_malformed"), ("//gateway.invalid", "base_url_malformed"),
               ("ftp://gateway.invalid", "base_url_malformed"), ("file:///etc/passwd", "base_url_malformed"),
               ("https://gateway.invalid/api?key=v", "base_url_malformed"), ("https://gateway.invalid/#f", "base_url_malformed"),
               ("https://gateway.invalid:0", "base_url_malformed"), ("https://gateway.invalid:99999", "base_url_malformed"),
               ("https://gateway.invalid/a/../b", "base_url_malformed"), ("https://gateway.invalid/a b", "base_url_malformed"),
               ("https://gateway.invalid\n", "base_url_malformed"), ("https://gateway.invalid\0", "base_url_malformed"),
               ("", "base_url_malformed"), (b"https://gateway.invalid", "base_url_malformed"), (7, "base_url_malformed"))
        for mode in AUTH_MODES:
            for url, cause in bad:
                with self.subTest(mode=mode, url=url):
                    route = ChildRoute(api_key=ROUTES[mode].api_key, base_url=url,
                                       provider_switches=ROUTES[mode].provider_switches)
                    self.assertEqual((URL, cause), self._refused("claude_code", home, auth_mode=mode, route=route, parent={}))

    def test_a_bracketed_base_url_host_must_be_an_ipv6_address(self) -> None:
        """Devin, "Invalid IPv6 gateway reaches child": the pattern alone let ``https://[:::]`` reach the child."""

        home = _home(self)
        bad = ("https://[:::]", "https://[1:2:3]", "http://[::g]", "https://[::1::]", "https://[1.2.3.4]", "https://[.]",
               "https://[12345::1]", "https://[1:2:3:4:5:6:7:8:9]", "https://[::1%eth0]", "https://[::1%25eth0]",
               "https://[:::]:8443/v1", "https://[]", "https://[::1")
        good = ("https://[::1]", "http://[::1]:4000", "https://[2001:db8::1]", "https://[2001:DB8::1]:8443/v1/",
                "https://[0:0:0:0:0:0:0:1]", "https://[::]", "https://[::ffff:127.0.0.1]")
        for mode in (API, CLOUD):
            route = lambda url: ChildRoute(api_key=ROUTES[mode].api_key, base_url=url,  # noqa: E731
                                           provider_switches=ROUTES[mode].provider_switches)
            for url in bad:
                with self.subTest(mode=mode, url=url):
                    self.assertEqual((URL, "base_url_malformed"),
                                     self._refused("claude_code", home, auth_mode=mode, route=route(url), parent={}))
            for url in good:
                with self.subTest(mode=mode, url=url):
                    env = build_child_env("claude_code", home=home, auth_mode=mode, route=route(url), parent={})
                    self.assertEqual(url, env[URL])
        # A subscription route: the value rule runs before trust and under full access; a trusted entry refuses the list.
        for url in bad:
            with self.subTest(mode=SUB, url=url):
                for more in ({}, {"full_access_acknowledged": True}):
                    self.assertEqual((URL, "base_url_malformed"), self._refused(
                        "claude_code", home, auth_mode=SUB, route=ChildRoute(base_url=url, **more), parent={}))
                self.assertEqual((None, "route_trusted_base_urls_malformed"), self._refused(
                    "claude_code", home, auth_mode=SUB, parent={},
                    route=ChildRoute(base_url="https://[::1]", trusted_base_urls=("https://[::1]", url))))
        for url in ("https://[::1]", "https://[2001:db8::1]"):
            with self.subTest(mode=SUB, url=url):
                route = ChildRoute(base_url=url, trusted_base_urls=(url,))
                self.assertEqual(url, build_child_env("claude_code", home=home, auth_mode=SUB, route=route, parent={})[URL])

    def test_a_bracketed_proxy_host_must_be_an_ipv6_address(self) -> None:
        home = _home(self)
        for name, scheme in (("HTTPS_PROXY", "http://"), ("http_proxy", ""), ("ALL_PROXY", "socks5://")):
            for host in ("[:::]", "[1:2:3]", "[::g]", "[1.2.3.4]"):
                for value in (scheme + host, scheme + host + ":3128"):
                    with self.subTest(name=name, value=value):
                        self.assertEqual((name, "proxy_malformed"), self._refused(
                            "claude_code", home, auth_mode=SUB, parent={"PATH": "/usr/bin", name: value}))
                        self.assertEqual((name, "proxy_malformed"), self._refused(
                            "codex", home, auth_mode=SUB, parent={"PATH": "/usr/bin"}, overrides={name: value}))
            for host in ("[::1]", "[2001:db8::1]:3128", "[::ffff:127.0.0.1]:3128/"):
                with self.subTest(name=name, value=scheme + host):
                    parent = {"PATH": "/usr/bin", name: scheme + host}
                    self.assertEqual(scheme + host, build_child_env("claude_code", home=home, auth_mode=SUB, parent=parent)[name])

    def test_ambient_keys_are_dropped_in_every_mode(self) -> None:
        home = _home(self)
        for engine, profile in ENGINE_PROFILES.items():
            for mode in AUTH_MODES:
                if engine == "codex" and mode == CLOUD:
                    continue  # refused: see test_codex_routes
                with self.subTest(engine=engine, mode=mode):
                    env = build_child_env(engine, home=home, auth_mode=mode, route=ROUTES[mode],
                                          parent={"PATH": "/usr/bin", **self.AMBIENT})
                    self.assertNotIn(FAKE, env.to_dict().values())  # no ambient key value, under any name
                    self.assertNotIn("https://ambient.invalid", env.to_dict().values())
                    route_set = {API: {profile.key_name}, CLOUD: {BEDROCK}, SUB: set()}[mode]
                    self.assertEqual(route_set, set(self.AMBIENT) & set(env))
                    self.assertLessEqual(set(self.AMBIENT) - route_set, set(env.dropped))

    def test_a_user_key_is_injected_only_on_an_api_key_route(self) -> None:
        home = _home(self)
        env = build_child_env("claude_code", home=home, auth_mode=API, route=ChildRoute(api_key=USER_KEY),
                              parent={"PATH": "/usr/bin", KEY: FAKE})
        self.assertEqual(USER_KEY, env[KEY])  # the caller's key, not the parent's
        self.assertEqual({KEY}, {name for name, value in env.to_dict().items() if value == USER_KEY})
        for mode, extra in ((SUB, ()), (CLOUD, (BEDROCK,))):
            for parent in ({}, {KEY: FAKE}):
                with self.subTest(mode=mode, parent=sorted(parent)):
                    route = ChildRoute(api_key=USER_KEY, provider_switches=extra)
                    self.assertEqual((KEY, "route_key_not_allowed"),
                                     self._refused("claude_code", home, auth_mode=mode, route=route, parent=parent))
        # An api_key route with no key refuses: the ambient key is not used, and the run does not fall back to a sign-in.
        for route in (None, ChildRoute(), ChildRoute(base_url="https://gateway.invalid")):
            with self.subTest(route=route):
                self.assertEqual((KEY, "route_key_missing"),
                                 self._refused("claude_code", home, auth_mode=API, route=route, parent={KEY: FAKE, OAUTH: FAKE}))
        for key in ("", "fake\0key", b"fake-bytes", 7):
            with self.subTest(key=key):
                self.assertEqual((KEY, "route_key_malformed"),
                                 self._refused("claude_code", home, auth_mode=API, route=ChildRoute(api_key=key), parent={}))

    def test_provider_switches_come_only_from_the_route(self) -> None:
        home = _home(self)
        leftovers = {"PATH": "/usr/bin", BEDROCK: "1", VERTEX: "true", FOUNDRY: "1"}
        for picked in ((BEDROCK,), (VERTEX,), (FOUNDRY,), (BEDROCK, VERTEX), [FOUNDRY], frozenset({VERTEX})):
            with self.subTest(picked=picked):
                env = build_child_env("claude_code", home=home, auth_mode=CLOUD, route=ChildRoute(provider_switches=picked),
                                      parent=leftovers)
                self.assertEqual(dict.fromkeys(picked, "1"), {n: env[n] for n in (BEDROCK, VERTEX, FOUNDRY) if n in env})
        for mode in (SUB, API):  # in the other modes an ambient switch is dropped and a picked one refuses
            env = build_child_env("claude_code", home=home, auth_mode=mode, route=ROUTES[mode], parent=leftovers)
            self.assertFalse({BEDROCK, VERTEX, FOUNDRY} & set(env))
            route = ChildRoute(api_key=ROUTES[mode].api_key, provider_switches=(BEDROCK,))
            self.assertEqual((None, "route_provider_not_allowed"),
                             self._refused("claude_code", home, auth_mode=mode, route=route, parent=leftovers))
        self.assertEqual((None, "route_provider_missing"), self._refused("claude_code", home, auth_mode=CLOUD, parent=leftovers))
        for unknown in ("CLAUDE_CODE_USE_OTHER", "ANTHROPIC_API_KEY", "PATH", "LD_PRELOAD", "claude_code_use_bedrock", ""):
            with self.subTest(unknown=unknown):
                route = ChildRoute(provider_switches=(BEDROCK, unknown))
                self.assertEqual((None, "route_provider_unknown"),
                                 self._refused("claude_code", home, auth_mode=CLOUD, route=route, parent={}))

    def test_the_route_is_its_own_input_not_a_mapping_or_an_override(self) -> None:
        home = _home(self)
        for route in ({"api_key": USER_KEY}, (USER_KEY,), USER_KEY, mock.Mock(api_key=USER_KEY, base_url=None, provider_switches=())):
            with self.subTest(route=type(route).__name__):
                self.assertEqual((None, "route_malformed"), self._refused("claude_code", home, auth_mode=API, route=route, parent={}))
        for switches in (BEDROCK, (1,), {BEDROCK: "1"}, None):
            with self.subTest(switches=switches):
                self.assertEqual((None, "route_malformed"), self._refused(
                    "claude_code", home, auth_mode=CLOUD, route=ChildRoute(provider_switches=switches), parent={}))
        # The override path is unchanged in every mode: it never carries a key, a base URL, a switch or the sign-in token.
        for mode in AUTH_MODES:
            for name in (KEY, "ANTHROPIC_AUTH_TOKEN", URL, BEDROCK, VERTEX, FOUNDRY, OAUTH):
                with self.subTest(mode=mode, name=name):
                    self.assertEqual((name, "override_denied"), self._refused(
                        "claude_code", home, auth_mode=mode, route=ROUTES[mode], parent={}, overrides={name: "fake-override"}))
        self.assertEqual(("CODEX_API_KEY", "override_denied"), self._refused(
            "codex", home, auth_mode=API, route=ROUTES[API], parent={}, overrides={"CODEX_API_KEY": "fake-override"}))

    def test_oauth_token_invariants_hold_in_every_mode(self) -> None:
        home = _home(self)
        for mode in AUTH_MODES:
            with self.subTest(mode=mode):
                # Absent in the parent stays absent: no mode and no route makes one.
                env = build_child_env("claude_code", home=home, auth_mode=mode, route=ROUTES[mode],
                                      parent={"PATH": "/usr/bin", "ANTHROPIC_OAUTH_TOKEN": FAKE, "ANTHROPIC_AUTH_TOKEN": FAKE})
                self.assertNotIn(OAUTH, env)
                self.assertNotIn(FAKE, env.to_dict().values())
                # Present in the parent is carried unchanged (on an api_key route the key outranks it, section 3.3).
                env = build_child_env("claude_code", home=home, auth_mode=mode, route=ROUTES[mode],
                                      parent={"PATH": "/usr/bin", OAUTH: FAKE})
                self.assertEqual(FAKE, env[OAUTH])
                self.assertNotIn(FAKE, repr(env))
                # An override naming it is refused, with or without one in the parent.
                for parent in ({}, {OAUTH: FAKE}):
                    self.assertEqual((OAUTH, "override_denied"), self._refused(
                        "claude_code", home, auth_mode=mode, route=ROUTES[mode], parent=parent, overrides={OAUTH: "fake-override"}))
        # A Codex child never gets it, in any mode it can run.
        for mode in (SUB, API):
            env = build_child_env("codex", home=home, auth_mode=mode, route=ROUTES[mode], parent={"PATH": "/usr/bin", OAUTH: FAKE})
            self.assertNotIn(OAUTH, env)
            self.assertNotIn(FAKE, env.to_dict().values())
        # The route has no field that could carry or store the token, and the module names it only as a passthrough.
        self.assertEqual({"api_key", "base_url", "provider_switches", "trusted_base_urls", "full_access_acknowledged"},
                         set(ChildRoute.__dataclass_fields__))
        for profile in ENGINE_PROFILES.values():
            self.assertNotIn(OAUTH, (profile.key_name, profile.base_url_name, *profile.provider_switches, *profile.allowed))
        with self.assertRaises(TypeError):
            ChildRoute(oauth_token=FAKE)

    def test_codex_routes(self) -> None:
        home = _home(self)
        parent = {"PATH": "/usr/bin", **self.AMBIENT, OAUTH: FAKE}
        env = build_child_env("codex", home=home, auth_mode=SUB, parent=parent)  # as before this change: nothing vendor-shaped
        self.assertFalse((set(self.AMBIENT) | {OAUTH}) & set(env))
        env = build_child_env("codex", home=home, auth_mode=API, route=ChildRoute(api_key=USER_KEY), parent=parent)
        self.assertEqual({"CODEX_API_KEY"}, (set(self.AMBIENT) | {OAUTH}) & set(env))
        self.assertEqual(USER_KEY, env["CODEX_API_KEY"])
        self.assertEqual(("CODEX_API_KEY", "route_key_missing"), self._refused("codex", home, auth_mode=API, parent=parent))
        self.assertEqual(("CODEX_API_KEY", "route_key_not_allowed"),
                         self._refused("codex", home, auth_mode=SUB, route=ChildRoute(api_key=USER_KEY), parent=parent))
        self.assertEqual((None, "route_base_url_unsupported"), self._refused(
            "codex", home, auth_mode=API, route=ChildRoute(api_key=USER_KEY, base_url="https://gateway.invalid"), parent=parent))
        for route in (None, ChildRoute(provider_switches=(BEDROCK,))):
            self.assertEqual((None, "auth_mode_unsupported"), self._refused("codex", home, auth_mode=CLOUD, route=route, parent=parent))

    def test_no_secret_in_repr_str_or_refusal_text(self) -> None:
        home = _home(self)
        secret, url_secret = "fake-user-entered-SECRET-0123456789", "fakepass-SECRET-9876"
        url = f"https://fakeuser:{url_secret}@gateway.invalid/api"
        routes = [ChildRoute(api_key=secret), ChildRoute(api_key=secret, base_url=url),
                  ChildRoute(api_key=secret, base_url="https://gateway.invalid"),
                  ChildRoute(api_key=secret, provider_switches=(BEDROCK,)), ChildRoute(api_key=secret, provider_switches=(secret,)),
                  ChildRoute(api_key=secret + "\0"), ChildRoute(base_url=url), ChildRoute(base_url=url, provider_switches=(VERTEX,))]
        texts: list[str] = []
        for route in routes:
            texts += [repr(route), str(route), f"{route}", f"{route!r:>80}", repr([route]), repr({"route": route})]
            self.assertTrue(repr(route).startswith("ChildRoute(api_key=<"))
        refusals = launched = 0
        for engine in ENGINE_PROFILES:
            for mode in (*AUTH_MODES, None, "unknown-mode"):
                for route in routes:
                    for overrides in (None, {KEY: secret}, {OAUTH: secret}):
                        try:
                            env = build_child_env(engine, home=home, auth_mode=mode, route=route, overrides=overrides,
                                                  parent={"PATH": "/usr/bin", OAUTH: FAKE})
                        except EnvRefused as refused:
                            refusals += 1
                            texts += [str(refused), repr(refused), repr(refused.args), str(refused.name), refused.cause]
                        else:
                            launched += 1
                            texts += [repr(env), str(env), repr(env.dropped), repr(sorted(env))]
                            self.assertEqual(secret, env[ENGINE_PROFILES[engine].key_name])  # the child does get it
        self.assertGreater(refusals, 50)
        self.assertGreater(launched, 1)
        for text in texts:
            self.assertNotIn(secret, text)
            self.assertNotIn(url_secret, text)
            self.assertNotIn(FAKE, text)

    def test_child_env_reads_no_keyring_and_no_key_file(self) -> None:
        # Key custody: the caller fetches the key (OS keyring, or the owner-only file); this package is handed it.
        allowed = {"__future__", "ipaddress", "os", "re", "shutil", "stat", "subprocess", "tempfile", "threading", "collections.abc",
                   "dataclasses", "pathlib", "types", "recursion_core.child_env.policy", "recursion_core.child_env.launch"}
        for module in (policy, launch):
            tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
            imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
            imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
            self.assertLessEqual(imported, allowed, module.__name__)
            calls = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
            self.assertNotIn("open", calls, module.__name__)
        home = _home(self)
        boom = AssertionError("child_env opened a file")
        with mock.patch("builtins.open", side_effect=boom), mock.patch("io.open", side_effect=boom), \
                mock.patch("os.open", side_effect=boom), mock.patch("os.read", side_effect=boom):
            for mode in AUTH_MODES:
                env = build_child_env("claude_code", home=home, auth_mode=mode, route=ROUTES[mode],
                                      parent={"PATH": "/usr/bin", "LANG": "C.UTF-8", OAUTH: FAKE})
                self.assertEqual(mode, env.auth_mode)
        self.assertEqual(USER_KEY, build_child_env("claude_code", home=home, auth_mode=API, route=ROUTES[API], parent={})[KEY])

    def test_disable_autoupdater_stays_off_the_claude_allowlist(self) -> None:
        # Owner call: a Claude child may update its own binary. The name is not carried, not set, and not settable.
        home = _home(self)
        for profile in ENGINE_PROFILES.values():
            self.assertNotIn("DISABLE_AUTOUPDATER", profile.allowed)
            self.assertNotIn("DISABLE_AUTOUPDATER", profile.passthrough)
        for mode in AUTH_MODES:
            with self.subTest(mode=mode):
                env = build_child_env("claude_code", home=home, auth_mode=mode, route=ROUTES[mode],
                                      parent={"PATH": "/usr/bin", "DISABLE_AUTOUPDATER": "1"})
                self.assertNotIn("DISABLE_AUTOUPDATER", env)
                self.assertIn("DISABLE_AUTOUPDATER", env.dropped)
                env = build_child_env("claude_code", home=home, auth_mode=mode, route=ROUTES[mode], parent={"PATH": "/usr/bin"})
                self.assertNotIn("DISABLE_AUTOUPDATER", env)
                self.assertEqual(("DISABLE_AUTOUPDATER", "override_not_allowed"), self._refused(
                    "claude_code", home, auth_mode=mode, route=ROUTES[mode], parent={}, overrides={"DISABLE_AUTOUPDATER": "1"}))


@unittest.skipIf(os.name == "nt", "POSIX rules")
class SubscriptionBaseUrlTests(unittest.TestCase):
    """Owner call (CSLR-D-011): a subscription base URL refuses unless official, trusted by the user or full access."""

    CUSTOM = "https://gateway.invalid/api"
    OFFICIAL = ("https://api.anthropic.com", "https://api.anthropic.com/", "https://api.anthropic.com:443",
                "HTTPS://API.Anthropic.COM", "https://Api.Anthropic.com:443/", "https://api.anthropic.com:0443",
                "https://api.anthropic.com/v1")
    LOOK_ALIKES = ("http://api.anthropic.com", "http://api.anthropic.com:443", "https://api.anthropic.com:8443",
                   "https://api.anthropic.com:80", "https://api.anthropic.com.evil", "https://api.anthropic.com.evil.com",
                   "https://api.anthropic.com.", "https://evil-api.anthropic.com", "https://xapi.anthropic.com",
                   "https://anthropic.com", "https://api.anthropic.co", "https://api-anthropic.com",
                   "https://api.anthropic.com.evil.com/api.anthropic.com", "https://evil.invalid/api.anthropic.com",
                   "https://api.anthropic.com-evil.invalid",
                   "https://[::1]", "https://127.0.0.1")
    UNTRUSTED = (URL, "route_base_url_untrusted")

    def _refused(self, home: str, mode: str = SUB, engine: str = "claude_code", **route: object) -> tuple[str | None, str]:
        fields = {"api_key": ROUTES[mode].api_key, "provider_switches": ROUTES[mode].provider_switches, **route}
        with self.assertRaises(EnvRefused) as caught:
            build_child_env(engine, home=home, auth_mode=mode, route=ChildRoute(**fields), parent={"PATH": "/usr/bin", OAUTH: FAKE})
        self.assertNotIn("://", str(caught.exception) + repr(caught.exception))
        return caught.exception.name, caught.exception.cause

    def _launched(self, home: str, mode: str = SUB, **route: object) -> ValidatedEnv:
        fields = {"api_key": ROUTES[mode].api_key, "provider_switches": ROUTES[mode].provider_switches, **route}
        return build_child_env("claude_code", home=home, auth_mode=mode, route=ChildRoute(**fields),
                               parent={"PATH": "/usr/bin", OAUTH: FAKE, URL: "https://ambient.invalid"})

    def test_subscription_with_no_base_url_launches(self) -> None:
        home = _home(self)
        for route in ({}, {"trusted_base_urls": ("https://gateway.invalid",)}, {"full_access_acknowledged": True}):
            with self.subTest(route=sorted(route)):
                env = self._launched(home, **route)
                self.assertNotIn(URL, env)
                self.assertEqual(FAKE, env[OAUTH])

    def test_the_official_origin_launches_without_trust(self) -> None:
        home = _home(self)
        for url in self.OFFICIAL:
            with self.subTest(url=url):
                env = self._launched(home, base_url=url)
                self.assertEqual(url, env[URL])  # the child gets the route's own spelling
                self.assertEqual(FAKE, env[OAUTH])

    def test_look_alikes_of_the_official_origin_refuse(self) -> None:
        home = _home(self)
        for url in self.LOOK_ALIKES:
            with self.subTest(url=url):
                self.assertEqual(self.UNTRUSTED, self._refused(home, base_url=url))
        # A user name or password refuses by the value rule, before any origin is compared and even when trusted.
        for url in ("https://user@api.anthropic.com", "https://api.anthropic.com@evil.invalid", "https://api.anthropic.com:443@evil.invalid"):
            for route in ({}, {"trusted_base_urls": ("https://evil.invalid", "https://api.anthropic.com")},
                          {"full_access_acknowledged": True}):
                with self.subTest(url=url, route=sorted(route)):
                    self.assertEqual((URL, "base_url_userinfo"), self._refused(home, base_url=url, **route))

    def test_an_untrusted_custom_url_refuses(self) -> None:
        home = _home(self)
        for url in (self.CUSTOM, "https://gateway.invalid", "http://127.0.0.1:4000", "https://[::1]:8443/v1/"):
            with self.subTest(url=url):
                self.assertEqual(self.UNTRUSTED, self._refused(home, base_url=url))
                self.assertEqual(self.UNTRUSTED, self._refused(home, base_url=url, trusted_base_urls=frozenset()))
                self.assertEqual(self.UNTRUSTED, self._refused(home, base_url=url, full_access_acknowledged=False))

    def test_the_same_url_passed_as_trusted_launches(self) -> None:
        home = _home(self)
        cases = ((self.CUSTOM, "https://gateway.invalid"), (self.CUSTOM, "https://gateway.invalid:443"),
                 (self.CUSTOM, "HTTPS://Gateway.INVALID/"), ("https://Gateway.invalid:443", "https://gateway.invalid"),
                 ("http://127.0.0.1:4000/v1", "http://127.0.0.1:4000"), ("http://localhost", "http://localhost:80"),
                 ("https://[::1]:8443/v1/", "https://[::1]:8443"))
        for url, entry in cases:
            for trusted in ((entry,), frozenset({entry}), ("https://other.invalid", entry)):
                with self.subTest(url=url, trusted=sorted(trusted)):
                    env = self._launched(home, base_url=url, trusted_base_urls=trusted)
                    self.assertEqual(url, env[URL])
                    self.assertEqual(FAKE, env[OAUTH])

    def test_a_trusted_list_holding_a_different_origin_still_refuses(self) -> None:
        home = _home(self)
        for entry in ("https://other.invalid", "http://gateway.invalid", "https://gateway.invalid:8443", "https://gateway.invalid.",
                      "https://gateway.invalid.evil", "https://sub.gateway.invalid", "https://invalid", "https://api.anthropic.com"):
            with self.subTest(entry=entry):
                self.assertEqual(self.UNTRUSTED, self._refused(home, base_url=self.CUSTOM, trusted_base_urls=(entry,)))
        # Another spelling of the same address is another origin: nothing is folded beyond case and the default port.
        self.assertEqual(self.UNTRUSTED, self._refused(home, base_url="https://[::1]", trusted_base_urls=("https://[0:0:0:0:0:0:0:1]",)))

    def test_a_malformed_trusted_list_refuses_in_every_mode(self) -> None:
        home = _home(self)
        malformed = (None, "https://gateway.invalid", ["https://gateway.invalid"], {"https://gateway.invalid": True},
                     (b"https://gateway.invalid",), (7,), (None,), ("",), ("gateway.invalid",), ("//gateway.invalid",),
                     ("ftp://gateway.invalid",), ("https://fakeuser@gateway.invalid",), ("https://gateway.invalid/api",),
                     ("https://gateway.invalid?k=v",), ("https://gateway.invalid:0",), ("https://gateway.invalid", "*"),
                     ("https://*.invalid",), ("https://gateway.invalid\n",))
        for trusted in malformed:
            for mode in AUTH_MODES:
                for url in (None, self.CUSTOM, "https://api.anthropic.com"):
                    with self.subTest(trusted=trusted, mode=mode, url=url):
                        self.assertEqual((None, "route_trusted_base_urls_malformed"),
                                         self._refused(home, mode, base_url=url, trusted_base_urls=trusted))

    def test_the_full_access_acknowledgment_launches_a_custom_url(self) -> None:
        home = _home(self)
        for url in (self.CUSTOM, "http://127.0.0.1:4000", "https://api.anthropic.com.evil"):
            with self.subTest(url=url):
                env = self._launched(home, base_url=url, full_access_acknowledged=True)
                self.assertEqual(url, env[URL])
                self.assertEqual(FAKE, env[OAUTH])
        # The URL value rule still applies under full access.
        for url, cause in (("gateway.invalid", "base_url_malformed"), ("https://gateway.invalid/?k=v", "base_url_malformed"),
                           ("https://fakeuser:fakepass@gateway.invalid", "base_url_userinfo")):
            with self.subTest(url=url):
                self.assertEqual((URL, cause), self._refused(home, base_url=url, full_access_acknowledged=True))

    def test_only_the_bool_true_is_an_acknowledgment(self) -> None:
        home = _home(self)
        for value in ("true", "True", "yes", "1", 1, 1.0, b"1", (True,), [True], {"ack": True}, None, 0, "", mock.Mock()):
            for mode in AUTH_MODES:
                for url in (None, self.CUSTOM):
                    with self.subTest(value=value, mode=mode, url=url):
                        self.assertEqual((None, "route_full_access_malformed"),
                                         self._refused(home, mode, base_url=url, full_access_acknowledged=value))

    def test_full_access_and_trust_are_never_read_from_the_parent_or_an_override(self) -> None:
        home = _home(self)
        route = ChildRoute(base_url=self.CUSTOM)
        ambient = {"PATH": "/usr/bin", URL: self.CUSTOM, "RECURSION_FULL_ACCESS": "1", "FULL_ACCESS": "1",
                   "RECURSION_FULL_ACCESS_ACKNOWLEDGED": "true", "RECURSION_TRUSTED_BASE_URLS": "https://gateway.invalid",
                   "TRUSTED_BASE_URLS": "https://gateway.invalid", "CLAUDE_CODE_DANGEROUSLY_SKIP_PERMISSIONS": "1"}
        with self.assertRaises(EnvRefused) as caught:
            build_child_env("claude_code", home=home, auth_mode=SUB, route=route, parent=ambient)
        self.assertEqual(self.UNTRUSTED, (caught.exception.name, caught.exception.cause))
        with mock.patch.dict(os.environ, ambient, clear=True), self.assertRaises(EnvRefused) as caught:
            build_child_env("claude_code", home=home, auth_mode=SUB, route=route)
        self.assertEqual(self.UNTRUSTED, (caught.exception.name, caught.exception.cause))
        # An override cannot name the base URL, and no override stands in for the route's two inputs.
        for name in (URL, "RECURSION_TRUSTED_BASE_URLS", "RECURSION_FULL_ACCESS"):
            with self.subTest(override=name), self.assertRaises(EnvRefused) as caught:
                build_child_env("claude_code", home=home, auth_mode=SUB, route=ChildRoute(), parent={}, overrides={name: self.CUSTOM})
            self.assertEqual((name, "override_not_allowed" if name == "RECURSION_FULL_ACCESS" else "override_denied"),
                             (caught.exception.name, caught.exception.cause))
        # Nothing is stored or opened to decide: the trusted list is only the tuple the caller passed.
        boom = AssertionError("child_env opened a file")
        with mock.patch("builtins.open", side_effect=boom), mock.patch("io.open", side_effect=boom), \
                mock.patch("os.open", side_effect=boom), mock.patch("os.read", side_effect=boom), \
                mock.patch("builtins.input", side_effect=AssertionError("child_env prompted")):
            self.assertEqual(self.UNTRUSTED, self._refused(home, base_url=self.CUSTOM))
            self.assertEqual(self.CUSTOM, self._launched(home, base_url=self.CUSTOM, trusted_base_urls=("https://gateway.invalid",))[URL])
            self.assertEqual(self.UNTRUSTED, self._refused(home, base_url=self.CUSTOM))  # a launch remembered nothing

    def test_api_key_and_cloud_provider_base_urls_are_unchanged(self) -> None:
        home = _home(self)
        for mode in (API, CLOUD):
            for url in (self.CUSTOM, "http://127.0.0.1:4000", "http://api.anthropic.com", "https://api.anthropic.com.evil",
                        "https://api.anthropic.com"):
                # No trust is needed, and a trusted list or an acknowledgment changes nothing.
                for route in ({}, {"trusted_base_urls": ("https://other.invalid",)}, {"full_access_acknowledged": True},
                              {"trusted_base_urls": ("https://gateway.invalid",), "full_access_acknowledged": True}):
                    with self.subTest(mode=mode, url=url, route=sorted(route)):
                        env = self._launched(home, mode, base_url=url, **route)
                        self.assertEqual(url, env[URL])
                        plain = self._launched(home, mode, base_url=url)
                        self.assertEqual(plain.to_dict(), env.to_dict())
            self.assertNotIn(URL, self._launched(home, mode, full_access_acknowledged=True, trusted_base_urls=("https://gateway.invalid",)))
        self.assertEqual(USER_KEY, self._launched(home, API, base_url=self.CUSTOM)[KEY])
        self.assertEqual("1", self._launched(home, CLOUD, base_url=self.CUSTOM)[BEDROCK])
        # The inputs do not loosen what a subscription route may carry.
        self.assertEqual((KEY, "route_key_not_allowed"), self._refused(home, api_key=USER_KEY, full_access_acknowledged=True))
        self.assertEqual((None, "route_provider_not_allowed"),
                         self._refused(home, provider_switches=(BEDROCK,), full_access_acknowledged=True))

    def test_codex_has_no_base_url_route_whatever_the_trust(self) -> None:
        home = _home(self)
        for route in ({}, {"trusted_base_urls": ("https://gateway.invalid",)}, {"full_access_acknowledged": True}):
            for url in (self.CUSTOM, "https://api.anthropic.com"):
                with self.subTest(route=sorted(route), url=url):
                    self.assertEqual((None, "route_base_url_unsupported"), self._refused(home, engine="codex", base_url=url, **route))
        env = build_child_env("codex", home=home, auth_mode=SUB, parent={"PATH": "/usr/bin", OAUTH: FAKE},
                              route=ChildRoute(trusted_base_urls=("https://gateway.invalid",), full_access_acknowledged=True))
        self.assertFalse({URL, OAUTH, "OPENAI_BASE_URL"} & set(env))

    def test_no_url_in_repr_str_or_refusal_text(self) -> None:
        home = _home(self)
        marks = ("gateway", "invalid", "anthropic.com", "://", "8443", "trusted-host", "secret-path")
        urls = (self.CUSTOM, "https://api.anthropic.com", "https://api.anthropic.com.evil", "http://trusted-host.invalid:8443/secret-path",
                "https://fakeuser:fakepass@gateway.invalid", "not a url")
        lists = ((), ("https://gateway.invalid",), ("https://trusted-host.invalid:8443", "https://gateway.invalid"),
                 ("https://gateway.invalid/secret-path",), "https://gateway.invalid")
        texts: list[str] = []
        refusals = launched = 0
        for url in (None, *urls):
            for trusted in lists:
                for ack in (False, True, "https://gateway.invalid"):
                    route = ChildRoute(base_url=url, trusted_base_urls=trusted, full_access_acknowledged=ack)
                    texts += [repr(route), str(route), f"{route}", repr([route]), repr({"route": route})]
                    for mode in (*AUTH_MODES, None):
                        try:
                            env = build_child_env("claude_code", home=home, auth_mode=mode, route=route, parent={"PATH": "/usr/bin"})
                        except EnvRefused as refused:
                            refusals += 1
                            texts += [str(refused), repr(refused), repr(refused.args), str(refused.name), refused.cause]
                        else:
                            launched += 1
                            texts += [repr(env), str(env), repr(env.dropped), repr(sorted(env))]
        self.assertGreater(refusals, 50)
        self.assertGreater(launched, 5)
        for text in texts:
            for mark in marks:
                self.assertNotIn(mark, text.lower())
        self.assertEqual("ChildRoute(api_key=<none>, base_url=<set>, provider_switches=<0>, trusted_base_urls=<2>, "
                         "full_access_acknowledged=<yes>)",
                         repr(ChildRoute(base_url=self.CUSTOM, trusted_base_urls=lists[2], full_access_acknowledged=True)))
        self.assertEqual("ChildRoute(api_key=<none>, base_url=<none>, provider_switches=<0>, trusted_base_urls=<?>, "
                         "full_access_acknowledged=<no>)",
                         repr(ChildRoute(trusted_base_urls="https://gateway.invalid", full_access_acknowledged="yes")))


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
        return build_child_env(engine, home=self.home, auth_mode=SUB, parent=parent)

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

    def test_an_api_key_route_reaches_popen_only_through_the_validated_env(self) -> None:
        parent = {"PATH": f"{self.bin}:/usr/bin:/bin", KEY: FAKE, URL: "https://ambient.invalid"}
        with mock.patch.dict(os.environ, {KEY: FAKE, URL: "https://ambient.invalid"}):
            env = build_child_env("claude_code", home=self.home, auth_mode=API, parent=parent,
                                  route=ChildRoute(api_key=USER_KEY, base_url="https://gateway.invalid/api"))
            spawn(["fake-engine", "-p"], env, popen=self.popen)
        child = self.popen.call_args.kwargs["env"]
        self.assertEqual((USER_KEY, "https://gateway.invalid/api"), (child[KEY], child[URL]))
        self.assertNotIn(FAKE, child.values())
        self.popen.reset_mock()
        with self.assertRaises(EnvRefused):  # a subscription route carrying a key never reaches Popen
            env = build_child_env("claude_code", home=self.home, auth_mode=SUB, parent=parent, route=ChildRoute(api_key=USER_KEY))
            spawn(["fake-engine", "-p"], env, popen=self.popen)
        self.popen.assert_not_called()

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
        env = build_child_env("codex", home=self.home, auth_mode=SUB, parent={"PATH": "/usr/bin:/bin"})
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
                    env = build_child_env("codex", home=self.home, auth_mode=SUB, parent=parent, overrides=bad)
                    spawn(["fake-engine"], env, popen=self.popen)
        self.popen.assert_not_called()
        env = build_child_env("codex", home=self.home, auth_mode=SUB, parent=parent, overrides={"LANG": "C.UTF-8"})
        spawn(["fake-engine"], env, popen=self.popen)
        self.assertEqual("C.UTF-8", self.popen.call_args.kwargs["env"]["LANG"])

    def test_real_child_sees_only_the_validated_env(self) -> None:
        """One real local child (/usr/bin/env, no engine): it prints exactly the validated names."""

        if not os.path.exists("/usr/bin/env"):
            self.skipTest("needs /usr/bin/env")
        with mock.patch.dict(os.environ, {"CODEX_API_KEY": FAKE, "OPENAI_API_KEY": FAKE}):
            env = build_child_env("codex", home=self.home, auth_mode=SUB, parent={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
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
