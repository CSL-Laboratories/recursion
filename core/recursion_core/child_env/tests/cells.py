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


def owned_tmpdir(prefix: str) -> str:
    """A fresh 0700 directory whose whole chain passes the owned-chain rule, whatever TMPDIR names.

    The process temp dir is tried first, then /tmp. With neither usable the fixtures cannot be built, and
    that is an error, not a skip: a cell must never pass or fail because of where the host put TMPDIR.

    Windows has no ``os.geteuid``, so the owned-chain rule cannot be evaluated. Skip rather than call
    it: a fixture must not pass by accepting a directory the POSIX rule would refuse, and it must not
    raise ``AttributeError``.
    """

    if os.name == "nt" or not hasattr(os, "geteuid") or not hasattr(os, "getuid"):
        raise unittest.SkipTest("POSIX rules")
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
        geteuid = getattr(os, "geteuid", None)
        # No euid: do not accept the temp root. A SID/DACL check is future work.
        if geteuid is None or st.st_uid != geteuid() or st.st_mode & 0o077:
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

    # C1-46 (PRODUCT.md 055cc7a5 section 3.8): the names the Nov 5 list dropped, now carried under the same rules.
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

    # The route base host is an endpoint the child uses. While a proxy is set, a NO_PROXY entry that matches it
    # refuses, including a CIDR that contains an IP literal. A CIDR does not match a domain name.
    gateway = ChildRoute(api_key=USER_KEY, base_url=base_url)
    ip_route = ChildRoute(api_key=USER_KEY, base_url="http://10.1.2.3:4000")
    add("no_proxy_route", "route_host", {"HTTPS_PROXY": PROXY, "NO_PROXY": "gateway.invalid"}, R("no_proxy_engine_host"),
        auth=api, route=gateway)
    add("no_proxy_route", "route_suffix", {"HTTPS_PROXY": PROXY, "NO_PROXY": "invalid"}, R("no_proxy_engine_host"),
        auth=api, route=gateway)
    add("no_proxy_route", "route_dot_domain", {"HTTPS_PROXY": PROXY, "NO_PROXY": ".invalid"}, R("no_proxy_engine_host"),
        auth=api, route=gateway)
    add("no_proxy_route", "route_cidr", {"HTTPS_PROXY": PROXY, "NO_PROXY": "10.0.0.0/8"}, R("no_proxy_engine_host"),
        auth=api, route=ip_route)
    add("no_proxy_route", "route_cidr_v6", {"HTTPS_PROXY": PROXY, "NO_PROXY": "::1/128"}, R("no_proxy_engine_host"),
        auth=api, route=ChildRoute(api_key=USER_KEY, base_url="http://[::1]:4000"))
    add("no_proxy_route", "route_wildcard", {"HTTPS_PROXY": PROXY, "NO_PROXY": "*.invalid"}, R("no_proxy_engine_host"),
        auth=api, route=gateway)
    add("no_proxy_route", "route_star", {"HTTPS_PROXY": PROXY, "NO_PROXY": "*"}, R("no_proxy_wildcard"),
        auth=api, route=gateway)
    add("no_proxy_route", "route_bracket_v6", {"HTTPS_PROXY": PROXY, "NO_PROXY": "[::1]"}, R("no_proxy_engine_host"),
        auth=api, route=ChildRoute(api_key=USER_KEY, base_url="http://[::1]:4000"))
    add("no_proxy_route", "route_host_trusted", {"HTTPS_PROXY": PROXY, "NO_PROXY": "gateway.invalid"},
        R("no_proxy_engine_host"), route=ChildRoute(base_url=base_url, trusted_base_urls=("https://gateway.invalid",)))
    add("no_proxy_route_ok", "cidr_other_net", {"HTTPS_PROXY": PROXY, "NO_PROXY": "192.168.0.0/16"},
        L(_same(NO_PROXY="192.168.0.0/16", **{url: "http://10.1.2.3:4000"})), auth=api, route=ip_route)
    add("no_proxy_route_ok", "cidr_domain", {"HTTPS_PROXY": PROXY, "NO_PROXY": "10.0.0.0/8"},
        L(_same(NO_PROXY="10.0.0.0/8", **{url: base_url})), auth=api, route=gateway)
    add("no_proxy_route_ok", "cidr_no_route", {"HTTPS_PROXY": PROXY, "NO_PROXY": "10.0.0.0/8,localhost"},
        L(_same(NO_PROXY="10.0.0.0/8,localhost")))
    add("no_proxy_route_ok", "host_without_proxy", {"NO_PROXY": "gateway.invalid"},
        L(_same(NO_PROXY="gateway.invalid", **{url: base_url})), auth=api, route=gateway)
    add("no_proxy_route_ok", "lookalike", {"HTTPS_PROXY": PROXY, "NO_PROXY": "notgateway.invalid,gateway.invalid.example"},
        L(_same(**{url: base_url})), auth=api, route=gateway)
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
