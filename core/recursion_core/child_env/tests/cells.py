"""The C2-37 / C2-38 cells as parent environments for ``build_child_env`` (CSL-785, candidate).

Cell names and shapes follow csl-research records 20261102-csl678-value-check-v3-01 (section 3)
and 20261103-csl678-envpath-path-noproxy-01 (sections 3.1 and 3.2). Every value is fake: no
credential, proxy or engine is real, and nothing is spawned. ``pub`` is a 0777 directory, ``op``
the operator's 0700 directory and ``grp`` a 0775 directory, all under one private scratch root.

Every cell builds its own directory chain. The "system" directories a cell puts on PATH (``usr/bin``,
``bin``, ``usr/local/bin``, ``usr/sbin``, ``sbin``) are fixture directories under ``op/sys``, never the
host's: a GitHub-hosted runner image makes ``/usr/local/bin`` mode 0777 and has ``/usr/bin`` links into
the 0777 ``/usr/share``, which the rules refuse (run 42733662905). The ``hosted`` cells keep that layout
as a refusal. The scratch root is made where its whole chain passes the owned-chain rule.

Each cell names its auth mode (``subscription`` unless the cell says otherwise) and, where the cell is
about a route, the ``ChildRoute`` a caller would pass after it fetched the user's key itself.

A cell expects either a refusal (``("refused", cause)``; cause None means any cause) or a launch
(``("launched", check)``, where ``check(env)`` returns an error string or None).
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from recursion_core.child_env import policy
from recursion_core.child_env.policy import ChildRoute, EnvRefused, build_child_env

N = 5
PROXY = "http://127.0.0.1:3128"
USER_KEY = "fake-user-entered-key-not-a-credential"  # what a caller would have fetched from the keyring
AMBIENT = "fake-ambient-value-not-a-credential"  # what the launching shell happens to hold


@dataclass(frozen=True)
class Cell:
    group: str
    name: str
    engine: str
    parent: dict
    expect: tuple
    note: str = ""
    auth_mode: str | None = "subscription"
    route: ChildRoute | None = None


POSIX_ONLY = ("POSIX-only: needs os.geteuid owner checks; child_env refuses platform_unsupported on Windows; "
              "covered by WindowsRefusalTests")


def owned_tmpdir(prefix: str) -> str:
    """A fresh 0700 directory whose whole chain passes the owned-chain rule, whatever TMPDIR names.

    The process temp dir is tried first, then /tmp. With neither usable the fixtures cannot be built, and
    that is an error, not a skip: a cell must never pass or fail because of where the host put TMPDIR.
    """

    if not policy._posix_supported():  # before mkdtemp: nothing is left behind where the owner checks cannot run
        raise unittest.SkipTest(POSIX_ONLY)
    for base in (tempfile.gettempdir(), "/tmp"):
        try:
            path = tempfile.mkdtemp(prefix=prefix, dir=os.path.realpath(base))
        except OSError:
            continue
        if policy._owned_chain(path):
            return path
        os.rmdir(path)
    raise RuntimeError("no temp base with a fully owned directory chain for the child_env fixtures")


class Scratch:
    """Fixture tree: pub (0777), op (0700), grp (0775), a route home, a system tree and attacker files."""

    def __init__(self) -> None:
        saved = os.umask(0o022)  # the modes below are the fixture's own, not the host umask's
        try:
            self._build()
        finally:
            os.umask(saved)

    def _build(self) -> None:
        self.root = Path(owned_tmpdir("csl785-cells-"))
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.pub, self.op, self.grp = self.root / "pub", self.root / "op", self.root / "grp"
        self.op.mkdir(mode=0o700)
        self.pub.mkdir()
        self.grp.mkdir()
        for name in ("ca.pem", "ca.crt", "zone"):
            (self.pub / name).write_text("fake\n", encoding="ascii")
        (self.pub / "certs").mkdir()
        (self.pub / "certs" / "ca.crt").write_text("fake\n", encoding="ascii")
        for tool in ("bash", "zsh", "git", "sleep", "python3"):
            _script(self.pub / tool)
        (self.op / "ca.pem").write_text("operator ca\n", encoding="ascii")
        (self.op / "key.pem").write_text("fake operator key\n", encoding="ascii")
        (self.op / "key.pem").chmod(0o600)
        (self.op / "d").mkdir()
        (self.op / "certs").mkdir()
        (self.op / "certs" / "ca.crt").write_text("operator ca\n", encoding="ascii")
        (self.op / "real").mkdir()
        _script(self.op / "real" / "bash")
        (self.op / "real" / "ca.crt").write_text("operator ca\n", encoding="ascii")
        (self.grp / "ca.pem").write_text("fake\n", encoding="ascii")
        (self.pub / "L").symlink_to(self.op / "d")  # trust_dotdot: a link anyone can re-point, then ..
        (self.pub / "PL").symlink_to(self.op / "real")  # path_link_repoint: safe today, re-pointable
        (self.pub / "x").mkdir()
        (self.pub / "x" / "bin").mkdir()
        _script(self.pub / "x" / "bin" / "bash")
        (self.op / "lnk").symlink_to(self.pub)  # path_link_pub
        (self.op / "P").symlink_to(self.pub / "x")  # path_parent_link
        (self.op / "bin").mkdir()
        (self.op / "bin" / "bash").symlink_to(self.pub / "bash")  # path_tool_link
        (self.op / "bin2").mkdir()
        (self.op / "bin2" / "python3").symlink_to(self.pub / "python3")  # path_tool_link_py
        (self.op / "obin").mkdir()
        _script(self.op / "obin" / "mytool")
        (self.op / "obinlink").symlink_to(self.op / "obin")
        (self.root / "bin").mkdir()  # path_rel target
        _script(self.root / "bin" / "bash")
        # C2-40 (#868, CX-3770): an owned outer link whose target passes through a mutable inner link in pub.
        (self.pub / "inner").symlink_to(self.op / "real")
        (self.op / "outer").symlink_to(self.pub / "inner")
        (self.pub / "inner_d").symlink_to(self.op / "d")
        (self.op / "outer_d").symlink_to(self.pub / "inner_d")
        # #320 review: a plain directory in pub that a ".." steps back out of, in a value and in a link's target.
        (self.pub / "hop").mkdir()
        (self.op / "bin3").mkdir()
        (self.op / "bin3" / "bash").symlink_to("../../pub/hop/../../op/sys/usr/bin/bash")  # dotdot_tool_link
        (self.op / "up").symlink_to("d/../certs/ca.crt")  # dotdot_ok_link: ".." through our own directories only
        # CX-3771: an owned alias to /proc/self/cwd.
        (self.op / "procalias").symlink_to("/proc/self/cwd")
        # The cells' own system tree, merged-usr style (bin -> usr/bin, sbin -> usr/sbin): every directory and
        # link is ours, so no cell's outcome depends on how the host owns /usr/local/bin, /usr/sbin or /usr/bin.
        self.sys = self.op / "sys"
        for sub in ("usr/bin", "usr/sbin", "usr/local/bin"):
            (self.sys / sub).mkdir(parents=True)
        (self.sys / "bin").symlink_to("usr/bin")
        (self.sys / "sbin").symlink_to("usr/sbin")
        for tool in ("bash", "sh", "git", "sleep", "env"):
            _script(self.sys / "usr" / "bin" / tool)
        # A sticky world-writable directory of our own, standing in for /var/tmp.
        self.sticky = self.root / "stk"
        self.sticky.mkdir()
        # The GitHub-hosted runner layout that failed run 42733662905 (ubuntu-24.04 image 20261101.360):
        # install-nodejs.sh runs "chmod -R 777 /usr/local/bin" and configure-system.sh "chmod -R 777 /usr/share",
        # and /usr/bin holds links into /usr/share (ant, dotnet, gradle, mvn, sbt).
        self.hosted = self.root / "hosted"
        for sub in ("usr/bin", "usr/local/bin", "usr/share/ant/bin"):
            (self.hosted / sub).mkdir(parents=True)
        _script(self.hosted / "usr" / "local" / "bin" / "node")
        _script(self.hosted / "usr" / "share" / "ant" / "bin" / "ant")
        (self.hosted / "usr" / "bin" / "ant").symlink_to("../share/ant/bin/ant")
        for loose in ("usr/local/bin/node", "usr/local/bin", "usr/share/ant/bin/ant", "usr/share/ant/bin",
                      "usr/share/ant", "usr/share"):
            (self.hosted / loose).chmod(0o777)
        self.sticky.chmod(0o1777)
        self.pub.chmod(0o777)
        self.grp.chmod(0o775)

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def _script(path: Path) -> None:
    path.write_text("#!/bin/sh\necho fake-attacker-tool\n", encoding="ascii")
    path.chmod(0o755)


def _replaced(*bad: str) -> Callable[[dict], str | None]:
    def check(env: dict) -> str | None:
        names = [name for name in ("TMPDIR", "TMP", "TEMP") if name in env]
        if not names:
            return "no temp name in child env"
        tmp = env[names[0]]
        st = os.lstat(tmp)
        if tmp in bad or any(env[name] != tmp for name in names):
            return f"temp not replaced: {tmp}"
        if st.st_uid != os.geteuid() or st.st_mode & 0o077:
            return "route temp root not private"
        return None

    return check


def _same(**expected: str) -> Callable[[dict], str | None]:
    def check(env: dict) -> str | None:
        wrong = [name for name, value in expected.items() if env.get(name) != value]
        return f"changed: {wrong}" if wrong else None

    return check


def _absent(*names: str) -> Callable[[dict], str | None]:
    def check(env: dict) -> str | None:
        present = [name for name in names if name in env]
        return f"present: {present}" if present else None

    return check


def _ok(env: dict) -> str | None:
    return None


def cells(s: Scratch) -> list[Cell]:
    pub, op, grp, root = str(s.pub), str(s.op), str(s.grp), str(s.root)
    sysd, sticky, hosted = str(s.sys), str(s.sticky), str(s.hosted)
    ubin, sbin_ = f"{sysd}/usr/bin", f"{sysd}/bin"  # the fixture's own /usr/bin and /bin (a link to it)
    rest = f"{ubin}:{sbin_}"
    base = {"PATH": rest, "LANG": "C.UTF-8"}
    R = lambda cause=None: ("refused", cause)  # noqa: E731
    L = lambda check=_ok: ("launched", check)  # noqa: E731
    cc = "claude_code"
    out: list[Cell] = []

    def add(group, name, parent, expect, engine=cc, note="", raw=False, auth="subscription", route=None):
        out.append(Cell(group, name, engine, dict(parent) if raw or "PATH" in parent else {**base, **parent}, expect, note,
                        auth, route))

    # C2-37: #776's 15 attack cells (value record section 3).
    add("value", "trust_extra", {"NODE_EXTRA_CA_CERTS": f"{pub}/ca.pem"}, R("value_chain_unsafe"))
    add("value", "trust_sslfile_sys", {"SSL_CERT_FILE": f"{pub}/ca.pem"}, R("value_chain_unsafe"))
    add("value", "trust_ssldir_sys", {"SSL_CERT_DIR": f"{pub}/certs"}, R("value_chain_unsafe"))
    add("value", "trust_py_sslfile", {"SSL_CERT_FILE": f"{pub}/ca.crt"}, R("value_chain_unsafe"))
    add("value", "trust_requests", {"REQUESTS_CA_BUNDLE": f"{pub}/ca.pem"}, R("value_chain_unsafe"))
    add("value", "certstore_bundled", {"CLAUDE_CODE_CERT_STORE": "bundled", "SSL_CERT_FILE": f"{pub}/ca.pem"}, R("value_chain_unsafe"))
    add("value", "certstore_default", {"SSL_CERT_FILE": f"{pub}/ca.pem"}, R("value_chain_unsafe"))
    add("value", "proxy_userinfo", {"HTTPS_PROXY": "http://fakeuser:fakepass@127.0.0.1:9"}, R("proxy_userinfo"))
    add("value", "noproxy_star", {"HTTPS_PROXY": PROXY, "NO_PROXY": "*"}, R("no_proxy_wildcard"))
    add("value", "tmp_pub", {"TMPDIR": pub}, L(_replaced(pub)), note="closed by replacement (N5h option A)")
    add("value", "tmp_sticky", {"TMPDIR": sticky}, L(_replaced(sticky)), note="closed by replacement (N5h option A)")
    add("value", "shell_pub", {"SHELL": f"{pub}/bash"}, R("value_chain_unsafe"))
    add("value", "tz_abs", {"TZ": f"{pub}/zone"}, R("value_chain_unsafe"))
    add("value", "tz_rel", {"TZ": "../../../../pub/zone"}, R("tz_relative_path"))
    add("value", "trust_dotdot", {"SSL_CERT_FILE": f"{pub}/L/../certs/ca.crt"}, R("value_chain_unsafe"))

    # C2-37: the three false-refusal cases, the clean baselines and the extras.
    certs = f"{op}/certs"
    add("ordinary", "ord_certdir_colon", {"SSL_CERT_DIR": f"{certs}:"}, L(_same(SSL_CERT_DIR=f"{certs}:")))
    add("ordinary", "ord_proxy_underscore", {"HTTPS_PROXY": "http://squid_proxy:3128"}, L(_same(HTTPS_PROXY="http://squid_proxy:3128")))
    add("ordinary", "ord_tmp_0775", {"TMPDIR": grp}, L(_replaced(grp)))
    add("baseline", "trust_none", {}, L())
    add("baseline", "trust_py_none", {}, L(), engine="codex")
    add("baseline", "trust_extra_safe", {"NODE_EXTRA_CA_CERTS": f"{op}/ca.pem"}, L(_same(NODE_EXTRA_CA_CERTS=f"{op}/ca.pem")))
    add("baseline", "shell_safe", {"SHELL": f"{sbin_}/bash"}, L(_same(SHELL=f"{sbin_}/bash")))
    add("baseline", "tz_utc", {"TZ": "UTC"}, L(_same(TZ="UTC")))
    add("baseline", "tmp_private", {"TMPDIR": op}, L(_replaced()))
    add("baseline", "tmp_sticky_careful", {"TMPDIR": sticky}, L(_replaced(sticky)))
    add("baseline", "proxy_plain", {"HTTPS_PROXY": PROXY}, L(_same(HTTPS_PROXY=PROXY)))
    empties = ("SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE", "SHELL", "TZ",
               "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
    add("baseline", "ord_empty", dict.fromkeys(empties + ("TMPDIR", "TMP", "TEMP"), ""), L(_same(**dict.fromkeys(empties, ""))))
    add("extra", "noproxy_star_noproxy", {"NO_PROXY": "*"}, L(_same(NO_PROXY="*")))
    add("extra", "dot_ok", {"SSL_CERT_FILE": f"{op}/./ca.pem"}, L())
    add("extra", "dotdot_ok", {"SSL_CERT_FILE": f"{op}/d/../ca.pem"}, L())
    add("extra", "ssldir_sticky", {"SSL_CERT_DIR": sticky}, R("value_chain_unsafe"))
    add("extra", "shell_proc", {"SHELL": "/proc/self/cwd/bash"}, R("value_perspective"))
    add("extra", "dot_link", {"SSL_CERT_FILE": f"{pub}/L/./ca.crt"}, R("value_chain_unsafe"))
    add("extra", "ca_group", {"SSL_CERT_FILE": f"{grp}/ca.pem"}, R("value_chain_unsafe"))
    add("extra", "secrets_dropped", {"ANTHROPIC_API_KEY": "fake-not-a-key", "CODEX_API_KEY": "fake-not-a-key",
                                     "AWS_SECRET_ACCESS_KEY": "fake"}, L(_absent("ANTHROPIC_API_KEY", "CODEX_API_KEY", "AWS_SECRET_ACCESS_KEY")))

    # C2-40 (#868 V18/V20/V25; ledger CX-3770, CX-3771): hidden inner links and owned /proc aliases.
    add("c2_40", "chain_shell", {"SHELL": f"{op}/outer/bash"}, R("value_chain_unsafe"))
    add("c2_40", "chain_path", {"PATH": f"{op}/outer:{rest}"}, R("value_chain_unsafe"))
    add("c2_40", "chain_dotdot_ca", {"SSL_CERT_FILE": f"{op}/outer_d/../certs/ca.crt"}, R("value_chain_unsafe"))
    add("c2_40", "alias_shell", {"SHELL": f"{op}/procalias/bash"}, R("value_perspective"))
    add("c2_40", "alias_path", {"PATH": f"{op}/procalias:{rest}"}, R("value_perspective"))
    add("c2_40", "alias_sslfile", {"SSL_CERT_FILE": f"{op}/procalias/ca.crt"}, R("value_perspective"))

    # #320 review (Codex thread on _resolve): pub/hop is a plain directory today, so no link is met, and the
    # ".." drops it from the resolved path. Whoever can write pub can swap hop for a link after the check.
    hop = f"{pub}/hop/../.."  # resolves to the scratch root
    add("dotdot", "dotdot_ca", {"SSL_CERT_FILE": f"{hop}/op/ca.pem"}, R("value_chain_unsafe"))
    add("dotdot", "dotdot_ssldir", {"SSL_CERT_DIR": f"{hop}/op/certs"}, R("value_chain_unsafe"))
    add("dotdot", "dotdot_shell", {"SHELL": f"{hop}/op/sys/usr/bin/bash"}, R("value_chain_unsafe"))
    add("dotdot", "dotdot_path", {"PATH": f"{hop}/op/obin:{rest}"}, R("value_chain_unsafe"))
    add("dotdot", "dotdot_tool_link", {"PATH": f"{op}/bin3:{rest}"}, R("path_tool_value_chain_unsafe"))
    up_path = f"{op}/obin/../obin:{rest}"
    add("dotdot_ok", "dotdot_ok_ssldir", {"SSL_CERT_DIR": f"{op}/d/../certs"}, L(_same(SSL_CERT_DIR=f"{op}/d/../certs")))
    add("dotdot_ok", "dotdot_ok_shell", {"SHELL": f"{ubin}/../bin/bash"}, L(_same(SHELL=f"{ubin}/../bin/bash")))
    add("dotdot_ok", "dotdot_ok_path", {"PATH": up_path}, L(_same(PATH=up_path)))
    add("dotdot_ok", "dotdot_ok_link", {"SSL_CERT_FILE": f"{op}/up"}, L(_same(SSL_CERT_FILE=f"{op}/up")))

    # C1-46 (PRODUCT.md d84cdd26 section 11.5): the names the Nov 5 list dropped, now carried under the same rules.
    socks = "socks5h://127.0.0.1:1080"
    add("c1_46", "allproxy_userinfo", {"ALL_PROXY": "socks5://fakeuser:fakepass@127.0.0.1:1080"}, R("proxy_userinfo"))
    add("c1_46", "allproxy_scheme", {"all_proxy": "ftp://127.0.0.1:21"}, R("proxy_malformed"), engine="codex")
    add("c1_46", "allproxy_noproxy_star", {"ALL_PROXY": socks, "NO_PROXY": "*"}, R("no_proxy_wildcard"), engine="codex")
    add("c1_46", "xdg_runtime_pub", {"XDG_RUNTIME_DIR": pub}, R("value_chain_unsafe"))
    add("c1_46", "client_cert_pub", {"CLAUDE_CODE_CLIENT_CERT": f"{pub}/ca.pem"}, R("value_chain_unsafe"))
    add("c1_46", "client_key_group", {"CLAUDE_CODE_CLIENT_KEY": f"{grp}/ca.pem"}, R("value_chain_unsafe"))
    add("c1_46", "cert_store_path", {"CLAUDE_CODE_CERT_STORE": f"bundled,{pub}/ca.pem"}, R("cert_store_malformed"))
    mtls = {"CLAUDE_CODE_CLIENT_CERT": f"{op}/ca.pem", "CLAUDE_CODE_CLIENT_KEY": f"{op}/key.pem",
            "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE": "fake-passphrase"}
    add("c1_46_ok", "allproxy_socks", {"ALL_PROXY": socks}, L(_same(ALL_PROXY=socks)))
    add("c1_46_ok", "all_proxy_lower", {"all_proxy": PROXY}, L(_same(all_proxy=PROXY)), engine="codex")
    add("c1_46_ok", "xdg_runtime_op", {"XDG_RUNTIME_DIR": op}, L(_same(XDG_RUNTIME_DIR=op)))
    add("c1_46_ok", "mtls_op", mtls, L(_same(**mtls)))
    add("c1_46_ok", "cert_store_bundled", {"CLAUDE_CODE_CERT_STORE": "bundled", "SSL_CERT_FILE": f"{op}/ca.pem"},
        L(_same(CLAUDE_CODE_CERT_STORE="bundled", SSL_CERT_FILE=f"{op}/ca.pem")))
    add("c1_46_ok", "mtls_not_codex", mtls, L(_absent(*mtls)), engine="codex")
    # PRODUCT.md 055cc7a5 section 3.3, Nov 8 fleet correction: the user's own Claude sign-in token stays as it is.
    add("auth_ok", "oauth_passthrough", {"CLAUDE_CODE_OAUTH_TOKEN": "fake-not-a-token"},
        L(_same(CLAUDE_CODE_OAUTH_TOKEN="fake-not-a-token")))
    add("auth_ok", "oauth_absent", {}, L(_absent("CLAUDE_CODE_OAUTH_TOKEN")))
    add("auth_ok", "oauth_not_codex", {"CLAUDE_CODE_OAUTH_TOKEN": "fake-not-a-token"},
        L(_absent("CLAUDE_CODE_OAUTH_TOKEN")), engine="codex")

    # CSLR-D-011, PRODUCT.md 055cc7a5 section 3.3: three explicit auth modes; the key, the base URL and the
    # provider switches come from the caller's route, never from the parent.
    key, url, sub, api, cloud = "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "subscription", "api_key", "cloud_provider"
    bedrock, vertex, foundry = "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"
    base_url = "https://gateway.invalid/api"
    ambient = {key: AMBIENT, "ANTHROPIC_AUTH_TOKEN": AMBIENT, url: "https://ambient.invalid", bedrock: "1", vertex: "1",
               foundry: "1", "CODEX_API_KEY": AMBIENT, "OPENAI_API_KEY": AMBIENT, "OPENAI_BASE_URL": "https://ambient.invalid"}
    gone = tuple(ambient)
    add("auth_mode", "mode_omitted", {}, R("auth_mode_missing"), auth=None)
    add("auth_mode", "mode_omitted_codex", {}, R("auth_mode_missing"), engine="codex", auth=None)
    add("auth_mode", "mode_unknown", {}, R("auth_mode_unknown"), auth="oauth")
    add("auth_mode", "mode_wrong_case", {}, R("auth_mode_unknown"), auth="API_KEY")
    add("auth_mode", "mode_empty", {}, R("auth_mode_unknown"), auth="")
    add("auth_route", "sub_key_refused", {}, R("route_key_not_allowed"), route=ChildRoute(api_key=USER_KEY))
    add("auth_route", "sub_switch_refused", {}, R("route_provider_not_allowed"), route=ChildRoute(provider_switches=(bedrock,)))
    add("auth_route", "key_missing", {key: AMBIENT}, R("route_key_missing"), auth=api,
        note="an ambient key is never the route's key")
    add("auth_route", "key_empty", {}, R("route_key_malformed"), auth=api, route=ChildRoute(api_key=""))
    add("auth_route", "key_switch_refused", {}, R("route_provider_not_allowed"), auth=api,
        route=ChildRoute(api_key=USER_KEY, provider_switches=(vertex,)))
    add("auth_route", "cloud_key_refused", {}, R("route_key_not_allowed"), auth=cloud,
        route=ChildRoute(api_key=USER_KEY, provider_switches=(bedrock,)))
    add("auth_route", "cloud_none_picked", {bedrock: "1"}, R("route_provider_missing"), auth=cloud,
        note="an ambient switch is never the user's pick")
    add("auth_route", "cloud_unknown_switch", {}, R("route_provider_unknown"), auth=cloud,
        route=ChildRoute(provider_switches=("CLAUDE_CODE_USE_OTHER",)))
    add("auth_route", "base_url_userinfo", {}, R("base_url_userinfo"),
        route=ChildRoute(base_url="https://fakeuser:fakepass@gateway.invalid"))
    add("auth_route", "base_url_no_scheme", {}, R("base_url_malformed"), auth=api,
        route=ChildRoute(api_key=USER_KEY, base_url="gateway.invalid/api"))
    add("auth_route", "base_url_query", {}, R("base_url_malformed"), auth=cloud,
        route=ChildRoute(base_url="https://gateway.invalid/api?k=v", provider_switches=(bedrock,)))
    add("auth_route", "codex_cloud_refused", {}, R("auth_mode_unsupported"), engine="codex", auth=cloud)
    add("auth_route", "codex_base_url_refused", {}, R("route_base_url_unsupported"), engine="codex", auth=api,
        route=ChildRoute(api_key=USER_KEY, base_url=base_url))
    add("auth_route", "codex_sub_key_refused", {}, R("route_key_not_allowed"), engine="codex", route=ChildRoute(api_key=USER_KEY))

    def both(*checks):
        return lambda env: next((error for error in (check(env) for check in checks) if error), None)

    token = {"CLAUDE_CODE_OAUTH_TOKEN": "fake-not-a-token"}
    add("auth_route_ok", "sub_ambient_dropped", {**ambient, **token}, L(both(_absent(*gone), _same(**token))))
    add("auth_route_ok", "sub_route_base_url", ambient, L(both(_same(**{url: base_url}), _absent(*(set(gone) - {url})))),
        route=ChildRoute(base_url=base_url, trusted_base_urls=("https://gateway.invalid",)),
        note="launches as before only because the caller passes the origin as trusted by the user")
    add("auth_route_ok", "key_injected", {**ambient, **token},
        L(both(_same(**{key: USER_KEY}, **token), _absent(*(set(gone) - {key})))), auth=api, route=ChildRoute(api_key=USER_KEY),
        note="the caller's key, not the ambient one; no ambient base URL; the sign-in token stays")
    add("auth_route_ok", "key_route_base_url", ambient, L(_same(**{key: USER_KEY, url: "http://127.0.0.1:4000"})), auth=api,
        route=ChildRoute(api_key=USER_KEY, base_url="http://127.0.0.1:4000"))
    add("auth_route_ok", "key_no_oauth_made", {}, L(both(_same(**{key: USER_KEY}), _absent("CLAUDE_CODE_OAUTH_TOKEN", url))),
        auth=api, route=ChildRoute(api_key=USER_KEY))
    add("auth_route_ok", "cloud_picked_only", {**ambient, **token},
        L(both(_same(**{bedrock: "1"}, **token), _absent(*(set(gone) - {bedrock})))), auth=cloud,
        route=ChildRoute(provider_switches=(bedrock,)), note="ambient Vertex and Foundry switches are not the user's pick")
    add("auth_route_ok", "cloud_not_ambient_value", {vertex: "ambient-leftover"}, L(_same(**{vertex: "1"})), auth=cloud,
        route=ChildRoute(provider_switches=(vertex,)))
    add("auth_route_ok", "cloud_route_base_url", ambient, L(_same(**{foundry: "1", url: base_url})), auth=cloud,
        route=ChildRoute(base_url=base_url, provider_switches=(foundry,)))
    add("auth_route_ok", "codex_sub_ambient_dropped", {**ambient, **token}, L(_absent(*gone, *token)), engine="codex")

    # Owner call (CSLR-D-011): a subscription base URL would receive the passed-through sign-in token, so it
    # refuses unless its origin is Anthropic's own, the caller passes it as trusted by the user, or the run
    # uses the acknowledged full-access profile. api_key and cloud_provider routes are not checked.
    official, untrusted, origin = "https://api.anthropic.com", R("route_base_url_untrusted"), "https://gateway.invalid"
    sub_url = lambda value, **more: ChildRoute(base_url=value, **more)  # noqa: E731
    add("sub_base_url", "custom_untrusted", token, untrusted, route=sub_url(base_url))
    add("sub_base_url", "ambient_is_not_trust", {**token, url: base_url, "RECURSION_FULL_ACCESS": "1",
                                                 "RECURSION_TRUSTED_BASE_URLS": origin}, untrusted, route=sub_url(base_url))
    add("sub_base_url", "official_http", token, untrusted, route=sub_url("http://api.anthropic.com"))
    add("sub_base_url", "official_other_port", token, untrusted, route=sub_url(official + ":8443"))
    add("sub_base_url", "official_suffix", token, untrusted, route=sub_url(official + ".evil"))
    add("sub_base_url", "official_suffix_com", token, untrusted, route=sub_url(official + ".evil.com"))
    add("sub_base_url", "official_trailing_dot", token, untrusted, route=sub_url(official + "."))
    add("sub_base_url", "official_userinfo", token, R("base_url_userinfo"), route=sub_url("https://user@api.anthropic.com"))
    add("sub_base_url", "trusted_other_origin", token, untrusted,
        route=sub_url(base_url, trusted_base_urls=("https://other.invalid", origin + ":8443", "http://gateway.invalid")))
    add("sub_base_url", "trusted_entry_malformed", token, R("route_trusted_base_urls_malformed"),
        route=sub_url(base_url, trusted_base_urls=("gateway.invalid",)))
    add("sub_base_url", "trusted_not_a_tuple", token, R("route_trusted_base_urls_malformed"),
        route=sub_url(base_url, trusted_base_urls=origin))
    add("sub_base_url", "full_access_truthy_string", token, R("route_full_access_malformed"),
        route=sub_url(base_url, full_access_acknowledged="true"))
    add("sub_base_url_ok", "none", token, L(both(_absent(url), _same(**token))))
    add("sub_base_url_ok", "official", token, L(_same(**{url: official}, **token)), route=sub_url(official))
    add("sub_base_url_ok", "official_upper_host", token, L(_same(**{url: "HTTPS://API.Anthropic.COM"})),
        route=sub_url("HTTPS://API.Anthropic.COM"))
    add("sub_base_url_ok", "official_explicit_443", token, L(_same(**{url: official + ":443/"})), route=sub_url(official + ":443/"))
    add("sub_base_url_ok", "trusted", token, L(_same(**{url: base_url}, **token)), route=sub_url(base_url, trusted_base_urls=(origin,)))
    add("sub_base_url_ok", "trusted_normalized", token, L(_same(**{url: base_url})),
        route=sub_url(base_url, trusted_base_urls=frozenset({"HTTPS://Gateway.Invalid:443/"})))
    add("sub_base_url_ok", "full_access", token, L(_same(**{url: base_url}, **token)),
        route=sub_url(base_url, full_access_acknowledged=True))
    add("sub_base_url_ok", "key_needs_no_trust", {}, L(_same(**{key: USER_KEY, url: base_url})), auth=api,
        route=ChildRoute(api_key=USER_KEY, base_url=base_url))
    add("sub_base_url_ok", "cloud_needs_no_trust", {}, L(_same(**{bedrock: "1", url: base_url})), auth=cloud,
        route=ChildRoute(base_url=base_url, provider_switches=(bedrock,)))
    # A bracketed host must be an IPv6 address (Devin, "Invalid IPv6 gateway reaches child"): the base URL, a
    # trusted entry and a proxy value refuse as malformed; a real address launches as before.
    for name, host in (("colons", "[:::]"), ("short", "[1:2:3]"), ("not_hex", "[::g]"), ("dots", "[1.2.3.4]")):
        add("bracket_host", f"base_url_{name}", {}, R("base_url_malformed"), auth=api,
            route=ChildRoute(api_key=USER_KEY, base_url=f"https://{host}"))
    add("bracket_host", "base_url_sub_full_access", token, R("base_url_malformed"),
        route=sub_url("http://[::g]:8443/v1", full_access_acknowledged=True))
    add("bracket_host", "trusted_entry", token, R("route_trusted_base_urls_malformed"),
        route=sub_url(base_url, trusted_base_urls=(origin, "https://[:::]")))
    add("bracket_host", "proxy_colons", {"HTTPS_PROXY": "http://[:::]:3128"}, R("proxy_malformed"))
    add("bracket_host", "proxy_short", {"http_proxy": "[1:2:3]:3128"}, R("proxy_malformed"), engine="codex")
    add("bracket_host", "allproxy_not_hex", {"ALL_PROXY": "socks5://[::g]:1080"}, R("proxy_malformed"))
    # A user name or password is still reported first (Devin, "Userinfo refusal lost for malformed hosts").
    add("bracket_host", "base_url_userinfo_first", {}, R("base_url_userinfo"), auth=api,
        route=ChildRoute(api_key=USER_KEY, base_url="https://user@[:::]"))
    add("bracket_host", "proxy_userinfo_first", {"HTTPS_PROXY": "http://u:p@[:::]:8080"}, R("proxy_userinfo"))
    add("bracket_host_ok", "base_url_loopback", {}, L(_same(**{key: USER_KEY, url: "https://[::1]:8443/v1/"})), auth=api,
        route=ChildRoute(api_key=USER_KEY, base_url="https://[::1]:8443/v1/"))
    add("bracket_host_ok", "base_url_sub_trusted", token, L(_same(**{url: "https://[2001:db8::1]"}, **token)),
        route=sub_url("https://[2001:db8::1]", trusted_base_urls=("https://[2001:DB8::1]:443",)))
    add("bracket_host_ok", "base_url_v4_mapped", {}, L(_same(**{key: USER_KEY, url: "http://[::ffff:127.0.0.1]:4000"})), auth=api,
        route=ChildRoute(api_key=USER_KEY, base_url="http://[::ffff:127.0.0.1]:4000"))
    add("bracket_host_ok", "proxy_loopback", {"HTTPS_PROXY": "http://[::1]:3128"}, L(_same(HTTPS_PROXY="http://[::1]:3128")))
    add("bracket_host_ok", "allproxy_address", {"ALL_PROXY": "socks5h://[2001:db8::1]:1080"},
        L(_same(ALL_PROXY="socks5h://[2001:db8::1]:1080")))
    add("auth_route_ok", "codex_key_injected", {**ambient, **token},
        L(both(_same(CODEX_API_KEY=USER_KEY), _absent(*(set(gone) - {"CODEX_API_KEY"}), *token))), engine="codex", auth=api,
        route=ChildRoute(api_key=USER_KEY))

    # C2-38: the 15 PATH attack cells (envpath record section 3.1); path_tool_link_py is open under the
    # default tool list by design of N5i and is reported, not counted as closed.
    add("path", "path_pub", {"PATH": f"{pub}:{rest}"}, R("value_chain_unsafe"))
    add("path", "path_noshell", {"PATH": f"{pub}:{rest}"}, R("value_chain_unsafe"))
    add("path", "path_tool_git", {"PATH": f"{pub}:{rest}", "SHELL": f"{sbin_}/bash"}, R("value_chain_unsafe"))
    add("path", "path_empty_lead", {"PATH": f":{rest}"}, R("path_empty_entry"))
    add("path", "path_empty_trail", {"PATH": f"{rest}:"}, R("path_empty_entry"))
    add("path", "path_dot", {"PATH": f".:{rest}"}, R("value_relative"))
    add("path", "path_rel", {"PATH": f"bin:{rest}"}, R("value_relative"))
    add("path", "path_link_pub", {"PATH": f"{op}/lnk:{rest}"}, R("value_chain_unsafe"))
    add("path", "path_parent_link", {"PATH": f"{op}/P/bin:{rest}"}, R("value_chain_unsafe"))
    add("path", "path_link_repoint", {"PATH": f"{pub}/PL:{rest}"}, R("value_chain_unsafe"))
    add("path", "path_tool_link", {"PATH": f"{op}/bin:{rest}"}, R("path_tool_value_chain_unsafe"))
    add("path_open", "path_tool_link_py", {"PATH": f"{op}/bin2:{rest}"}, L(),
        note="open under N5i's default tool list (envpath record 3.1); PATH_TOOLS=None closes it")
    add("path", "path_group", {"PATH": f"{grp}:{rest}"}, R("value_chain_unsafe"))
    add("path", "path_sticky", {"PATH": f"{sticky}:{rest}"}, R("value_chain_unsafe"))
    add("path", "path_proc", {"PATH": f"/proc/self/cwd:{rest}"}, R("value_perspective"))
    ok_operator = f"{op}/obin:{sysd}/usr/local/bin:{ubin}:{sbin_}:{sysd}/usr/sbin:{sysd}/sbin"
    ok_missing = f"{sysd}/usr/local/bin:{ubin}:{sbin_}:{sysd}/snap/bin:{sysd}/usr/local/games:{op}/none"
    slashes = f"{ubin}/:/{ubin}:{sbin_}//:{ubin}"
    add("path_ok", "path_ok_operator", {"PATH": ok_operator}, L(_same(PATH=ok_operator)))
    add("path_ok", "path_ok_missing", {"PATH": ok_missing}, L(_same(PATH=ok_missing)))
    add("path_ok", "path_ok_link", {"PATH": f"{op}/obinlink:{rest}"}, L(_same(PATH=f"{op}/obinlink:{rest}")))
    add("path_ok", "path_ok_slashes", {"PATH": slashes}, L(_same(PATH=slashes)))
    add("path_ok", "path_ok_unset", {"LANG": "C.UTF-8"}, L(_absent("PATH")), raw=True)

    # The hosted-runner layout (run 42733662905): a PATH directory anyone can write is refused, as path_pub is,
    # also when it is spelled usr/local/bin. A link from an owned bin directory to a file anyone can write is
    # outside N5i's default tool list, like path_tool_link_py; PATH_TOOLS=None refuses it by the file rule.
    add("hosted", "hosted_local_bin_0777", {"PATH": f"{op}/obin:{hosted}/usr/local/bin:{rest}"}, R("value_chain_unsafe"))
    add("hosted", "hosted_share_tool", {"PATH": f"{hosted}/usr/bin:{rest}"}, L(),
        note="open under N5i's default tool list; PATH_TOOLS=None closes it (file rule)")

    # C2-38: the 16 hostile NO_PROXY cells and 6 ordinary lists (envpath record section 3.2).
    hostile = [
        ("np_c_tld", cc, "com"), ("np_c_dottld", cc, ".com"), ("np_c_dom", cc, "anthropic.com"),
        ("np_c_dotdom", cc, ".anthropic.com"), ("np_c_host", cc, "api.anthropic.com"), ("np_c_oauth", cc, ".claude.com"),
        ("np_x_tld", "codex", "com"), ("np_x_dottld", "codex", ".com"), ("np_x_dom", "codex", "openai.com"),
        ("np_x_dotdom", "codex", ".openai.com"), ("np_x_host", "codex", "auth.openai.com"), ("np_x_chatgpt", "codex", "chatgpt.com"),
        ("np_c_glob", cc, "*.anthropic.com"), ("np_c_port", cc, "api.anthropic.com:443"),
        ("np_c_space_star", cc, "localhost *"), ("np_c_space_host", cc, "localhost api.anthropic.com"),
    ]
    for name, engine, value in hostile:
        cause = "no_proxy_wildcard" if name == "np_c_space_star" else "no_proxy_engine_host"
        add("no_proxy", name, {"HTTPS_PROXY": PROXY, "NO_PROXY": value}, R(cause), engine=engine)
    corp = "localhost,127.0.0.1,.corp.example"
    ordinary = [
        ("np_ok_corp_c", cc, corp, True), ("np_ok_corp_x", "codex", corp, True),
        ("np_ok_docs", cc, "localhost 192.168.1.1 example.com .example.com", True),
        ("np_ok_other_engine", cc, ".openai.com", True), ("np_ok_lookalike", cc, "notanthropic.com,anthropic.com.example", True),
        ("np_ok_noproxy", cc, "api.anthropic.com", False),
    ]
    for name, engine, value, with_proxy in ordinary:
        parent = {"NO_PROXY": value, **({"HTTPS_PROXY": PROXY} if with_proxy else {})}
        add("no_proxy_ok", name, parent, L(_same(NO_PROXY=value)), engine=engine)

    # CSL-839: the route's own base host is an endpoint too. While a proxy is set, a NO_PROXY entry that matches
    # it refuses (no_proxy_route_host), by the same matcher as the engine hosts; an IP-literal host is also
    # matched as an address and by a CIDR block that holds it. A "*" or engine-host entry keeps its own cause.
    keyed = lambda value: ChildRoute(api_key=USER_KEY, base_url=value)  # noqa: E731
    sub_gw, v4_url, v6_url = "https://llm.gateway.invalid", "http://10.1.2.3:4000", "https://[2001:db8::1]:8443/v1/"
    route_hits = [
        ("exact", base_url, "gateway.invalid"), ("dot_domain", sub_gw, ".gateway.invalid"),
        ("bare_domain", sub_gw, "gateway.invalid"), ("tld", base_url, "invalid"), ("glob", sub_gw, "*.gateway.invalid"),
        ("scheme", base_url, "https://gateway.invalid"), ("port", base_url, "gateway.invalid:443"),
        ("other_port", base_url, "gateway.invalid:8080"), ("upper", base_url, "GATEWAY.Invalid"),
        ("trailing_dot", base_url, "gateway.invalid."), ("url_upper", "https://Gateway.INVALID/api", "gateway.invalid"),
        ("url_trailing_dot", "https://gateway.invalid./api", "gateway.invalid"),
        ("v4", v4_url, "10.1.2.3"), ("v4_port", v4_url, "10.1.2.3:4000"), ("v4_other_port", v4_url, "10.1.2.3:9"),
        ("v4_cidr", v4_url, "10.0.0.0/8"), ("v4_cidr_host_bits", v4_url, "10.1.2.3/8"), ("v4_cidr_half", v4_url, "0.0.0.0/1"),
        ("v6_bracket", v6_url, "[2001:db8::1]"), ("v6_bracket_port", v6_url, "[2001:db8::1]:8443"),
        ("v6_bare", v6_url, "2001:db8::1"), ("v6_spelling", v6_url, "2001:db8:0:0:0:0:0:1"), ("v6_cidr", v6_url, "2001:db8::1/128"),
        ("v4_mapped", "http://[::ffff:10.1.2.3]:4000", "10.0.0.0/8"),
        ("one_of_many", base_url, "localhost,127.0.0.1 .corp.example, gateway.invalid"),
        # A host that only looks like loopback is not exempt: a longer name, a subdomain, the next address or block.
        ("like_localhost_com", "http://localhost.example.com:4000", "localhost.example.com"),
        ("like_localhost_evil", "http://localhost.evil", ".evil"), ("like_mylocalhost", "http://mylocalhost:4000", "mylocalhost"),
        ("like_sub_localhost", "http://foo.localhost:4000", "localhost"),
        ("like_v4_name", "http://127.0.0.1.example.com", "example.com"), ("like_v6_two", "http://[::2]:4000", "::2"),
        ("like_v4_128", "http://128.0.0.1:4000", "128.0.0.1"), ("like_v4_126", "http://126.255.255.255", "126.0.0.0/8"),
        ("like_v4_mapped_128", "http://[::ffff:128.0.0.1]:4000", "128.0.0.1"),
        # Only "::ffff:" carries an IPv4 address here: an IPv4-compatible or NAT64 spelling of 127.0.0.1 is not
        # loopback, so it refuses by design (fail closed).
        ("like_v4_compatible", "http://[::127.0.0.1]:4000", "::127.0.0.1"),
        ("like_nat64", "http://[64:ff9b::7f00:1]:4000", "64:ff9b::7f00:1"),
        ("like_nat64_cidr", "http://[64:ff9b::7f00:1]:4000", "64:ff9b::/97"),
    ]
    for name, value, entry in route_hits:
        add("no_proxy_route", f"npr_{name}", {"HTTPS_PROXY": PROXY, "NO_PROXY": entry}, R("no_proxy_route_host"), auth=api,
            route=keyed(value))
    for name, parent in (("all_proxy_only", {"ALL_PROXY": socks, "NO_PROXY": "gateway.invalid"}),
                         ("http_proxy_lower", {"http_proxy": PROXY, "NO_PROXY": "gateway.invalid"}),
                         ("lower_name", {"HTTPS_PROXY": PROXY, "no_proxy": "gateway.invalid"})):
        add("no_proxy_route", f"npr_{name}", parent, R("no_proxy_route_host"), auth=api, route=keyed(base_url))
    add("no_proxy_route", "npr_sub_trusted", {**token, "HTTPS_PROXY": PROXY, "NO_PROXY": "gateway.invalid"},
        R("no_proxy_route_host"), route=sub_url(base_url, trusted_base_urls=(origin,)))
    add("no_proxy_route", "npr_cloud", {"HTTPS_PROXY": PROXY, "NO_PROXY": ".invalid"}, R("no_proxy_route_host"), auth=cloud,
        route=ChildRoute(base_url=base_url, provider_switches=(bedrock,)))
    # Which cause wins: "*" and an engine host are checked first, in both names, so they report as before.
    add("no_proxy_route", "npr_star_first", {"HTTPS_PROXY": PROXY, "NO_PROXY": "gateway.invalid *"}, R("no_proxy_wildcard"),
        auth=api, route=keyed(base_url))
    add("no_proxy_route", "npr_engine_first", {"HTTPS_PROXY": PROXY, "NO_PROXY": "gateway.invalid", "no_proxy": ".anthropic.com"},
        R("no_proxy_engine_host"), auth=api, route=keyed(base_url))
    add("no_proxy_route", "npr_official_is_engine", {"HTTPS_PROXY": PROXY, "NO_PROXY": "api.anthropic.com"},
        R("no_proxy_engine_host"), route=sub_url(official))
    # A loopback route exempts the route-host check only: "*" and an engine host refuse as before.
    add("no_proxy_route", "npr_lo_star", {"HTTPS_PROXY": PROXY, "NO_PROXY": "*"}, R("no_proxy_wildcard"), auth=api,
        route=keyed("http://127.0.0.1:4000"))
    add("no_proxy_route", "npr_lo_engine", {"HTTPS_PROXY": PROXY, "NO_PROXY": "localhost,api.anthropic.com"},
        R("no_proxy_engine_host"), auth=api, route=keyed("http://localhost:4000"))
    # Owner call, 2026-11-10: a loopback route host is exempt, so NO_PROXY may name a gateway on this machine.
    loopback_ok = [
        ("localhost", "http://localhost:4000", "localhost"), ("localhost_upper_dot", "http://LOCALHOST.:4000", ".localhost"),
        ("v4", "http://127.0.0.1:4000", "127.0.0.1"), ("v4_other", "http://127.1.2.3:4000", "127.0.0.0/8"),
        ("v4_short", "http://127.1:4000", "127.0.0.1:4000"), ("v4_hex", "http://0x7f.0.0.1", "127.0.0.1"),
        ("v4_decimal", "http://2130706433", "127.0.0.1"), ("v4_last", "http://127.255.255.254", "127.255.255.254"),
        ("v6", "http://[::1]:4000", "::1"), ("v6_port", "http://[::1]:4000", "[::1]:4000"),
        ("v6_spelling", "http://[0:0:0:0:0:0:0:1]", "::1"), ("v4_mapped", "http://[::ffff:127.0.0.1]:4000", "127.0.0.1"),
        ("v4_mapped_hex", "http://[::ffff:7f00:1]", "127.0.0.0/8"),
        ("usual_list", "http://127.0.0.1:4000", "localhost,127.0.0.1,::1,127.0.0.0/8"),
        ("v4_glob", "http://127.0.0.1:4000", "127.*"), ("v4_half", "http://127.0.0.1:4000", "0.0.0.0/1"),
    ]
    for name, value, entry in loopback_ok:
        add("no_proxy_route_ok", f"npr_lo_{name}", {"HTTPS_PROXY": PROXY, "NO_PROXY": entry},
            L(_same(**{"NO_PROXY": entry, url: value})), auth=api, route=keyed(value))
    route_ok = [
        ("no_proxy_set", base_url, "gateway.invalid", False), ("unrelated", base_url, corp, True),
        ("lookalike", base_url, "notgateway.invalid,gateway.invalid.evil", True),
        ("child_of_host", base_url, "llm.gateway.invalid", True), ("empty", base_url, "", True),
        ("cidr_is_not_a_name", base_url, "10.0.0.0/8,0.0.0.0/1,2000::/3", True), ("cidr_other_net", v4_url, "192.168.0.0/16", True),
        ("v4_other", v4_url, "10.1.2.30,110.1.2.3,10.1.2.4:4000", True), ("v6_other", v6_url, "[2001:db8::2],2001:db8::1:0,::1,10.0.0.0/8", True),
    ]
    for name, value, entry, with_proxy in route_ok:
        parent = {"NO_PROXY": entry, **({"HTTPS_PROXY": PROXY} if with_proxy else {})}
        add("no_proxy_route_ok", f"npr_ok_{name}", parent, L(_same(**{"NO_PROXY": entry, url: value})), auth=api, route=keyed(value))
    for name, engine in (("no_base_url", cc), ("no_base_url_codex", "codex")):
        add("no_proxy_route_ok", f"npr_ok_{name}", {"HTTPS_PROXY": PROXY, "NO_PROXY": "gateway.invalid,10.0.0.0/8"},
            L(both(_same(NO_PROXY="gateway.invalid,10.0.0.0/8"), _absent(url))), engine=engine, auth=api,
            route=ChildRoute(api_key=USER_KEY))

    # CSL-839, owner call 2026-11-10: an all-address CIDR block is a "*". While a proxy is set it refuses with
    # no_proxy_wildcard on any route: no base URL (either engine), a gateway, a loopback gateway. All-address is
    # an IPv4 block of prefix 0, or an IPv6 block that holds all of ::ffff:0:0/96 (every IPv4-mapped address);
    # the other two IPv4-mapping blocks, unions and unparseable entries follow below.
    all_address = [
        ("v4", "0.0.0.0/0"), ("v4_host_bits", "10.0.0.0/0"), ("v4_scheme", "http://0.0.0.0/0"),
        ("v4_port", "0.0.0.0:80/0"), ("v6", "::/0"), ("v6_bracket", "[::]/0"), ("v6_long", "0:0:0:0:0:0:0:0/0"),
        ("v6_host_bits", "2001:db8::1/0"), ("v6_scheme", "https://[::]/0"), ("mapped", "::ffff:0:0/96"),
        ("mapped_dotted", "::ffff:0.0.0.0/96"), ("mapped_upper", "[::FFFF:0:0]/96"), ("mapped_host_bits", "::ffff:10.1.2.3/96"),
        ("mapped_95", "::fffe:0:0/95"), ("mapped_80", "::/80"), ("mapped_64", "::/64"), ("mapped_1", "::/1"),
    ]
    no_base = ChildRoute(api_key=USER_KEY)
    all_routes = [("none", cc, no_base), ("none_codex", "codex", no_base), ("gateway", cc, keyed(base_url)),
                  ("v4_gateway", cc, keyed(v4_url)), ("localhost", cc, keyed("http://localhost:4000")),
                  ("v6_loopback", cc, keyed("http://[::1]:4000"))]
    for name, entry in all_address:
        for route_name, engine, route in all_routes:
            add("no_proxy_all", f"npa_{name}_{route_name}", {"HTTPS_PROXY": PROXY, "NO_PROXY": entry}, R("no_proxy_wildcard"),
                engine=engine, auth=api, route=route)
    # Mixed lists, the lowercase name and every proxy name; it wins over a route-host entry, as "*" does.
    all_mixed = [
        ("usual_list", {"HTTPS_PROXY": PROXY, "NO_PROXY": "localhost,0.0.0.0/0"}, keyed("http://localhost:4000")),
        ("usual_list_v6", {"HTTPS_PROXY": PROXY, "NO_PROXY": "localhost,127.0.0.1 ::1, ::/0"}, keyed("http://[::1]:4000")),
        ("lower_name", {"HTTPS_PROXY": PROXY, "no_proxy": "localhost,0.0.0.0/0"}, no_base),
        ("lower_name_mapped", {"https_proxy": PROXY, "no_proxy": "::ffff:0:0/96"}, keyed("http://localhost:4000")),
        ("both_names", {"HTTPS_PROXY": PROXY, "NO_PROXY": corp, "no_proxy": "::/0"}, no_base),
        ("all_proxy_only", {"ALL_PROXY": socks, "NO_PROXY": "0.0.0.0/0"}, no_base),
        ("route_host_too", {"HTTPS_PROXY": PROXY, "NO_PROXY": "gateway.invalid,0.0.0.0/0"}, keyed(base_url)),
        ("route_host_other_name", {"HTTPS_PROXY": PROXY, "NO_PROXY": "gateway.invalid", "no_proxy": "::/0"}, keyed(base_url)),
        ("engine_host_after", {"HTTPS_PROXY": PROXY, "NO_PROXY": "0.0.0.0/0,api.anthropic.com"}, no_base),
    ]
    for name, parent, route in all_mixed:
        add("no_proxy_all", f"npa_mixed_{name}", parent, R("no_proxy_wildcard"), auth=api, route=route)
    add("no_proxy_all", "npa_mixed_sub", {**token, "HTTPS_PROXY": PROXY, "NO_PROXY": "0.0.0.0/0"}, R("no_proxy_wildcard"))
    add("no_proxy_all", "npa_mixed_codex_sub", {"HTTPS_PROXY": PROXY, "NO_PROXY": "localhost,::/0"}, R("no_proxy_wildcard"),
        engine="codex")
    # Not all-address: a block or blocks that hold less than every IPv4 address, and a bare address.
    # None refuses with no base URL; with no proxy set nothing refuses.
    narrower = [
        ("v4_8", "10.0.0.0/8"), ("v4_low_half", "0.0.0.0/1"), ("v4_high_half", "128.0.0.0/1"),
        ("v4_gap", "0.0.0.0/1,192.0.0.0/2"), ("v6_high_half", "8000::/1"), ("v6_global", "2000::/3"),
        ("mapped_97", "::ffff:0:0/97"), ("nat64_97", "64:ff9b::/97"), ("compatible_97", "::/97"), ("nat64_other_96", "64:ff9a::/96"),
        ("v4_address", "0.0.0.0"), ("v6_address", "::"),
    ]
    for name, entry in narrower:
        for route_name, engine in (("none", cc), ("none_codex", "codex")):
            add("no_proxy_all_ok", f"npa_ok_{name}_{route_name}", {"HTTPS_PROXY": PROXY, "NO_PROXY": entry},
                L(both(_same(NO_PROXY=entry), _absent(url))), engine=engine, auth=api, route=no_base)
    for name, entry in (("v4", "0.0.0.0/0"), ("v6", "::/0"), ("mapped", "::ffff:0:0/96"), ("usual_list", "localhost,0.0.0.0/0")):
        for route_name, engine in (("none", cc), ("none_codex", "codex")):
            add("no_proxy_all_ok", f"npa_ok_noproxy_{name}_{route_name}", {"NO_PROXY": entry},
                L(both(_same(NO_PROXY=entry), _absent(url))), engine=engine, auth=api, route=no_base)
        for route_name, value in (("gateway", base_url), ("v4_gateway", v4_url), ("localhost", "http://localhost:4000")):
            add("no_proxy_all_ok", f"npa_ok_noproxy_{name}_{route_name}", {"no_proxy": entry},
                L(_same(**{"no_proxy": entry, url: value})), auth=api, route=keyed(value))
    # A narrower block keeps the route-host CIDR behaviour: it refuses only when it holds the route host.
    add("no_proxy_all", "npa_narrower_holds_host", {"HTTPS_PROXY": PROXY, "NO_PROXY": "192.0.0.0/2,0.0.0.0/1"},
        R("no_proxy_route_host"), auth=api, route=keyed(v4_url))
    add("no_proxy_all", "npa_narrower_mapped_holds_host", {"HTTPS_PROXY": PROXY, "NO_PROXY": "::ffff:0:0/97"},
        R("no_proxy_route_host"), auth=api, route=keyed(v4_url))
    for name, value, entry in (("other_half", v4_url, "128.0.0.0/1,8000::/1"), ("v6_other_half", v6_url, "8000::/1,0.0.0.0/1"),
                               ("loopback_gap", "http://127.0.0.1:4000", "0.0.0.0/1,192.0.0.0/2"),
                               ("name_host", base_url, "0.0.0.0/1,192.0.0.0/2,2000::/3")):
        add("no_proxy_all_ok", f"npa_ok_narrower_{name}", {"HTTPS_PROXY": PROXY, "NO_PROXY": entry},
            L(_same(**{"NO_PROXY": entry, url: value})), auth=api, route=keyed(value))

    # CSL-839, owner call 2026-11-10 (2c): 64:ff9b::/96 (NAT64) and ::/96 (IPv4-compatible) carry every IPv4
    # address too, as ::ffff:0:0/96 does. A block that holds all of one of the three is a "*" on any route.
    mapping = [
        ("nat64", "64:ff9b::/96"), ("nat64_bracket", "[64:ff9b::]/96"), ("nat64_dotted", "64:ff9b::0.0.0.0/96"),
        ("nat64_upper", "64:FF9B::/96"), ("nat64_host_bits", "64:ff9b::7f00:1/96"), ("nat64_95", "64:ff9b::/95"),
        ("nat64_32", "64:ff9b::/32"), ("compatible", "::/96"), ("compatible_dotted", "::0.0.0.0/96"),
        ("compatible_bracket", "[::]:443/96"), ("compatible_95", "::/95"), ("compatible_81", "::/81"),
    ]
    for name, entry in mapping:
        for route_name, engine, route in all_routes:
            add("no_proxy_mapping", f"npm_{name}_{route_name}", {"HTTPS_PROXY": PROXY, "NO_PROXY": entry},
                R("no_proxy_wildcard"), engine=engine, auth=api, route=route)

    # (2a): the union of the blocks counts as one block. Halves, quarters, any order, any separator, duplicates
    # and overlaps; a bare address joins as its single-address block. NO_PROXY and no_proxy are one combined
    # list (the no_proxy_combined cells below).
    last_v4, last_mapped = "255.255.255.255", "::ffff:ffff:ffff"
    but_last_v4 = ",".join(map(str, policy._ALL_IPV4.address_exclude(ipaddress.ip_network(last_v4))))
    but_last_mapped = ",".join(map(str, policy._IPV4_MAPPING_BLOCKS[0].address_exclude(ipaddress.ip_network(last_mapped))))
    unions = [
        ("v4_halves", "0.0.0.0/1,128.0.0.0/1"), ("v4_quarters", "0.0.0.0/2,64.0.0.0/2,128.0.0.0/2,192.0.0.0/2"),
        ("v4_whitespace", "0.0.0.0/1 128.0.0.0/1"), ("v4_in_a_list", "localhost, 128.0.0.0/1 .corp.example  0.0.0.0/1,::1"),
        ("v4_duplicates", "0.0.0.0/1,0.0.0.0/1,10.0.0.0/8,128.0.0.0/1"), ("v4_overlaps", "0.0.0.0/1,64.0.0.0/2,128.0.0.0/2,128.0.0.0/1"),
        ("v4_three", "0.0.0.0/1,128.0.0.0/2,192.0.0.0/2"), ("v4_unordered", "192.0.0.0/2,0.0.0.0/1,128.0.0.0/2"),
        ("v4_host_bits", "10.1.2.3/1,200.1.2.3:80/1"), ("v4_bare_address", f"{but_last_v4},{last_v4}"),
        ("mapped_halves", "::ffff:0:0/97,::ffff:8000:0/97"), ("mapped_spellings", "::ffff:0.0.0.0/97 [::FFFF:128.0.0.0]/97"),
        ("mapped_bare_address", f"{but_last_mapped},[{last_mapped}]"),
        ("nat64_halves", "64:ff9b::/97,64:ff9b::8000:0/97"), ("compatible_halves", "::/97,::8000:0/97"),
    ]
    for name, entry in unions:
        for route_name, engine, route in all_routes:
            add("no_proxy_union", f"npu_{name}_{route_name}", {"HTTPS_PROXY": PROXY, "NO_PROXY": entry},
                R("no_proxy_wildcard"), engine=engine, auth=api, route=route)
    add("no_proxy_union", "npu_lower_name", {"HTTPS_PROXY": PROXY, "NO_PROXY": corp, "no_proxy": "128.0.0.0/1,0.0.0.0/1"},
        R("no_proxy_wildcard"), auth=api, route=no_base)
    add("no_proxy_union", "npu_route_host_too", {"HTTPS_PROXY": PROXY, "NO_PROXY": "128.0.0.0/1,0.0.0.0/1"},
        R("no_proxy_wildcard"), auth=api, route=keyed(v4_url))
    add("no_proxy_union", "npu_engine_host_first", {"HTTPS_PROXY": PROXY, "NO_PROXY": "0.0.0.0/1,api.anthropic.com,128.0.0.0/1"},
        R("no_proxy_engine_host"), auth=api, route=no_base)
    # No cover: a gap or the bare address left out.
    union_gaps = [
        ("v4_gap", {"NO_PROXY": "0.0.0.0/1,192.0.0.0/2"}), ("v4_no_bare_address", {"NO_PROXY": but_last_v4}),
        ("mapped_no_bare_address", {"NO_PROXY": but_last_mapped}), ("mapped_gap", {"NO_PROXY": "::ffff:0:0/97,::ffff:c000:0/98"}),
        ("v4_gap_two_names", {"NO_PROXY": "0.0.0.0/1", "no_proxy": "192.0.0.0/2"}),
        ("mapped_gap_two_names", {"NO_PROXY": "::ffff:0:0/97", "no_proxy": "::ffff:c000:0/98"}),
    ]
    for name, lists in union_gaps:
        for route_name, engine in (("none", cc), ("none_codex", "codex")):
            add("no_proxy_union_ok", f"npu_ok_{name}_{route_name}", {"HTTPS_PROXY": PROXY, **lists},
                L(both(_same(**lists), _absent(url))), engine=engine, auth=api, route=no_base)

    # Owner call, 2026-11-10 (2a): NO_PROXY and no_proxy are one combined list, NO_PROXY's entries first. A cover
    # split across the two names refuses as one in a single name does, on any route, in either order.
    v4_quarters = ("0.0.0.0/2,128.0.0.0/2", "64.0.0.0/2,192.0.0.0/2")
    split = [
        ("v4_halves", "0.0.0.0/1", "128.0.0.0/1"), ("v4_halves_swapped", "128.0.0.0/1", "0.0.0.0/1"),
        ("v4_quarters", *v4_quarters), ("v4_quarters_swapped", *reversed(v4_quarters)),
        ("v4_in_lists", f"{corp},0.0.0.0/1", "localhost 128.0.0.0/1"), ("v4_bare_address", but_last_v4, last_v4),
        ("mapped_halves", "::ffff:0:0/97", "::ffff:8000:0/97"), ("mapped_halves_swapped", "::ffff:8000:0/97", "::ffff:0:0/97"),
        ("mapped_spellings", "::ffff:0.0.0.0/97", "[::FFFF:128.0.0.0]/97"),
        ("nat64_halves", "64:ff9b::/97", "64:ff9b::8000:0/97"), ("compatible_halves", "::/97", "::8000:0/97"),
    ]
    for name, upper, lower in split:
        for route_name, engine, route in all_routes:
            add("no_proxy_combined", f"npc_{name}_{route_name}", {"HTTPS_PROXY": PROXY, "NO_PROXY": upper, "no_proxy": lower},
                R("no_proxy_wildcard"), engine=engine, auth=api, route=route)
    # Which cause wins is decided in combined order: an engine-host, "*" or unparseable entry before the entry
    # that completes the cover keeps its own cause, and the cover wins over one after it.
    combined_first = [
        ("engine_host_first", cc, {"NO_PROXY": "0.0.0.0/1,api.anthropic.com", "no_proxy": "128.0.0.0/1"}, "no_proxy_engine_host"),
        ("engine_host_first_codex", "codex", {"NO_PROXY": "0.0.0.0/1 .openai.com", "no_proxy": "128.0.0.0/1"}, "no_proxy_engine_host"),
        ("engine_host_after", cc, {"NO_PROXY": "0.0.0.0/1", "no_proxy": "128.0.0.0/1,api.anthropic.com"}, "no_proxy_wildcard"),
        ("star_first", cc, {"NO_PROXY": "0.0.0.0/1,*", "no_proxy": "128.0.0.0/1"}, "no_proxy_wildcard"),
        ("star_before_unparseable", cc, {"NO_PROXY": "*", "no_proxy": "10.0.0.0/33"}, "no_proxy_wildcard"),
        ("unparseable_before_star", cc, {"NO_PROXY": "10.0.0.0/33", "no_proxy": "*"}, "no_proxy_unparseable"),
        ("unparseable_first", cc, {"NO_PROXY": "0.0.0.0/1,example.com/", "no_proxy": "128.0.0.0/1"}, "no_proxy_unparseable"),
        ("unparseable_after", "codex", {"NO_PROXY": "0.0.0.0/1", "no_proxy": "128.0.0.0/1,example.com/"}, "no_proxy_wildcard"),
    ]
    for name, engine, lists, cause in combined_first:
        add("no_proxy_combined", f"npc_{name}", {"HTTPS_PROXY": PROXY, **lists}, R(cause), engine=engine, auth=api, route=no_base)
    add("no_proxy_combined", "npc_route_host_too", {"HTTPS_PROXY": PROXY, "NO_PROXY": "0.0.0.0/1", "no_proxy": "128.0.0.0/1"},
        R("no_proxy_wildcard"), auth=api, route=keyed(v4_url))
    add("no_proxy_combined", "npc_all_proxy_only", {"ALL_PROXY": socks, "NO_PROXY": "128.0.0.0/1", "no_proxy": "0.0.0.0/1"},
        R("no_proxy_wildcard"), auth=api, route=no_base)
    for route_name, engine in (("none", cc), ("none_codex", "codex")):  # no proxy set: nothing to bypass
        lists = {"NO_PROXY": "0.0.0.0/1", "no_proxy": "128.0.0.0/1"}
        add("no_proxy_combined_ok", f"npc_ok_noproxy_{route_name}", lists, L(both(_same(**lists), _absent(url))), engine=engine,
            auth=api, route=no_base)

    # Still open, for after Nov 11 (owner call, 2026-11-10; not closed here): the union never mixes the two
    # families, never joins halves of two different mapping blocks, and knows no IPv4-embedding prefix but the
    # three in _IPV4_MAPPING_BLOCKS. Each of these launches with no base URL.
    still_open = [
        ("two_families", {"NO_PROXY": "0.0.0.0/1,::ffff:8000:0/97"}),
        ("two_families_two_names", {"NO_PROXY": "0.0.0.0/1", "no_proxy": "::ffff:8000:0/97"}),
        ("two_blocks", {"NO_PROXY": "::ffff:0:0/97,64:ff9b::8000:0/97"}),
        ("two_blocks_two_names", {"NO_PROXY": "::ffff:0:0/97", "no_proxy": "64:ff9b::8000:0/97"}),
        ("nat64_local_use", {"NO_PROXY": "64:ff9b:1::/48"}), ("nat64_next_96", {"NO_PROXY": "64:ff9a::/96"}),
        ("six_to_four", {"NO_PROXY": "2002::/16"}), ("nat64_site", {"NO_PROXY": "2001:db8:64::/96"}),
    ]
    for name, lists in still_open:
        for route_name, engine in (("none", cc), ("none_codex", "codex")):
            add("no_proxy_open", f"npo_{name}_{route_name}", {"HTTPS_PROXY": PROXY, **lists},
                L(both(_same(**lists), _absent(url))), engine=engine, auth=api, route=no_base,
                note="open: mixed families, two mapping blocks and other IPv4-embedding prefixes are post-Nov-11 work")

    # Owner calls, 2026-11-10 (1, 2b, 2c): an unparseable entry refuses with its own cause, no_proxy_unparseable
    # (fail closed: a matcher may read it some other way). First, an entry with a "/" that is no CIDR block;
    # that takes in a name with a path or a trailing slash.
    slash = [
        ("zero_slash_zero", "0/0"), ("v4_prefix_33", "10.0.0.0/33"), ("v6_prefix_129", "::/129"), ("name_prefix", "abc/8"),
        ("no_prefix", "10.0.0.0/"), ("no_address", "/8"), ("minus_prefix", "0.0.0.0/-0"), ("two_slashes", "0.0.0.0/0/0"),
        ("name_slash", "example.com/"), ("url_slash", "http://example.com/"), ("dot_name_path", ".corp/path"),
        ("engine_name_slash", "api.anthropic.com/"), ("localhost_slash", "localhost/"),
        ("in_a_list", "localhost,127.0.0.1 10.0.0.0/33,.corp.example"),
        # Devin, on 7cd75440; owner call, 2026-11-10: more than one "/" is never a block (the port strip dropped
        # "/path" and read the first as 10.0.0.0/8), and a ":port" before the "/" must be a decimal port 1-65535.
        ("port_path_prefix", "10.1.2.3:8080/path/8"), ("three_parts", "a/b/c"), ("two_prefixes", "10.0.0.0/8/8"),
        ("leading_slashes", "//8"), ("url_two_slashes", "http://h/a/b"), ("port_not_decimal", "1.2.3.4:x/8"),
        ("port_zero", "1.2.3.4:0/8"), ("port_over", "1.2.3.4:65536/8"),
    ]
    # (2b): what follows the last "/" must be a plain decimal prefix length. A netmask or hostmask, a leading
    # zero, a sign, hex and a non-ASCII digit are not one.
    suffix = [
        ("netmask_all", "0.0.0.0/0.0.0.0"), ("hostmask_all", "0.0.0.0/255.255.255.255"), ("netmask", "10.0.0.0/255.0.0.0"),
        ("hostmask", "10.0.0.0/0.255.255.255"), ("leading_zero", "10.0.0.0/08"), ("double_zero", "0.0.0.0/00"),
        ("plus", "10.0.0.0/+8"), ("hex", "::/0x0"), ("arabic_indic_digit", "10.0.0.0/٨"),
    ]
    # (2c): any entry with a "%": an IPv6 zone id, with or without a prefix, a percent escape, a name.
    percent = [
        ("zone_block", "fe80::1%eth0/64"), ("zone_address", "fe80::1%eth0"), ("zone_escaped", "[fe80::1%25eth0]"),
        ("alone", "%"), ("name_escape", "host%2f"), ("in_a_list", "localhost,fe80::1%eth0 .corp.example"),
    ]
    slash_routes = [("none", cc, no_base), ("none_codex", "codex", no_base), ("gateway", cc, keyed(base_url)),
                    ("localhost", cc, keyed("http://localhost:4000"))]
    for group, short, entries in (("no_proxy_slash", "nps", slash), ("no_proxy_suffix", "npx", suffix),
                                  ("no_proxy_percent", "npp", percent)):
        for name, entry in entries:
            for route_name, engine, route in slash_routes:
                add(group, f"{short}_{name}_{route_name}", {"HTTPS_PROXY": PROXY, "NO_PROXY": entry},
                    R("no_proxy_unparseable"), engine=engine, auth=api, route=route)
    # no proxy set: nothing to bypass
    for name, entry in (("zero_slash_zero", "0/0"), ("name_slash", "example.com/"), ("netmask", "10.0.0.0/255.0.0.0"),
                        ("zone", "fe80::1%eth0")):
        for route_name, engine in (("none", cc), ("none_codex", "codex")):
            add("no_proxy_slash_ok", f"nps_ok_noproxy_{name}_{route_name}", {"NO_PROXY": entry},
                L(both(_same(NO_PROXY=entry), _absent(url))), engine=engine, auth=api, route=no_base)
    # A decimal port before the one "/" is dropped as before: 1.2.3.4:80/8 is the block 1.0.0.0/8 and launches.
    for route_name, engine, route in slash_routes:
        add("no_proxy_slash_ok", f"nps_ok_port_block_{route_name}", {"HTTPS_PROXY": PROXY, "NO_PROXY": "1.2.3.4:80/8"},
            L(_same(NO_PROXY="1.2.3.4:80/8")), engine=engine, auth=api, route=route)
    return out


def run_cell(cell: Cell, home: str) -> tuple[str, str | None]:
    """One trial: ("refused", cause) or ("launched", check error or None)."""

    try:
        env = build_child_env(cell.engine, home=home, auth_mode=cell.auth_mode, route=cell.route, parent=cell.parent)
    except EnvRefused as refused:
        return "refused", refused.cause
    return "launched", cell.expect[1](env.to_dict()) if cell.expect[0] == "launched" else None


def passed(cell: Cell, outcome: tuple[str, str | None]) -> bool:
    kind, detail = outcome
    if cell.expect[0] == "refused":
        return kind == "refused" and (cell.expect[1] is None or detail == cell.expect[1])
    return kind == "launched" and detail is None


def run_all(n: int = N) -> list[dict]:
    """Run every cell n times; one summary row per cell."""

    s = Scratch()
    try:
        rows = []
        for cell in cells(s):
            outcomes = [run_cell(cell, str(s.home)) for _ in range(n)]
            rows.append({
                "group": cell.group,
                "cell": cell.name,
                "engine": cell.engine,
                "auth_mode": cell.auth_mode,
                "expect": cell.expect[0] + (":" + cell.expect[1] if isinstance(cell.expect[1], str) else ""),
                "outcomes": sorted({f"{k}:{d}" if d else k for k, d in outcomes}),
                "pass": sum(passed(cell, o) for o in outcomes),
                "n": n,
            })
        rows.append(_scan_switch(s, n, "path_scan", "path_tool_link_py", "path_tool_value_chain_unsafe"))
        rows.append(_scan_switch(s, n, "hosted_scan", "hosted_share_tool", "path_tool_value_file_unsafe"))
        return rows
    finally:
        s.close()


def _scan_switch(s: Scratch, n: int, group: str, name: str, cause: str) -> dict:
    cell = next(c for c in cells(s) if c.name == name)
    saved = policy.PATH_TOOLS
    policy.PATH_TOOLS = None
    try:
        outcomes = [run_cell(cell, str(s.home)) for _ in range(n)]
    finally:
        policy.PATH_TOOLS = saved
    ok = [o == ("refused", cause) for o in outcomes]
    return {"group": group, "cell": f"{name}[PATH_TOOLS=None]", "engine": cell.engine, "auth_mode": cell.auth_mode,
            "expect": f"refused:{cause}", "outcomes": sorted({f"{k}:{d}" for k, d in outcomes}),
            "pass": sum(ok), "n": n}


if __name__ == "__main__":
    rows = run_all(int(sys.argv[1]) if len(sys.argv) > 1 else N)
    for row in rows:
        print(json.dumps(row, sort_keys=True))
    groups: dict[str, list[int]] = {}
    for row in rows:
        total = groups.setdefault(row["group"], [0, 0])
        total[0] += row["pass"] == row["n"]
        total[1] += 1
    print("# cells passing n/n per group: " + ", ".join(f"{g} {a}/{b}" for g, (a, b) in groups.items()))
    sys.exit(0 if all(row["pass"] == row["n"] for row in rows) else 1)
