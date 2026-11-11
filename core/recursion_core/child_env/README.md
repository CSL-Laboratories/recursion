# core/recursion_core/child_env (CSL-785, candidate)

New-core child environment for engine children. Candidate code: not yet called by a
running core, and not reviewed or accepted.

Spec: CSLR-D-012, PRODUCT.md `d84cdd26a4de2366f3c5d34c4aec5215f822a494` (SHA-256
`e1d5fb8c02594b2ca88281113ffc14e6f3125daded2a00d45739cfb9d2ed04dd`), section 3.3.

- `policy.build_child_env(engine, home=..., auth_mode=..., route=..., parent=...)` builds the
  child environment from an exact-name allowlist per engine profile (`claude_code`, `codex`),
  checks the values, and returns a `ValidatedEnv` or raises `EnvRefused(name, cause)` before
  any spawn. `auth_mode` is required (see [Auth mode and the route](#auth-mode-and-the-route)).
  Values are never included in errors or `repr`. `overrides=` may only set a name
  already on the engine's allowlist (never `HOME`, `PATH` or a credential name); overrides
  are merged first and then meet exactly the same value, PATH, NO_PROXY and temp rules.
- Allowlist: the 29 names the Nov 5 tree carried plus `ALL_PROXY`, `all_proxy` and
  `XDG_RUNTIME_DIR` (32 names, both engines), and for Claude Code also
  `CLAUDE_CODE_CLIENT_CERT`, `CLAUDE_CODE_CLIENT_KEY`, `CLAUDE_CODE_CLIENT_KEY_PASSPHRASE` and
  `CLAUDE_CODE_CERT_STORE` (PRODUCT.md 055cc7a5 section 3.8, C1-46). `ALL_PROXY` meets the proxy
  rule with SOCKS schemes allowed and counts as a set proxy for the `NO_PROXY` rule; the client
  cert and key meet the file rule when inherited; `XDG_RUNTIME_DIR` meets the directory rule;
  `CLAUDE_CODE_CERT_STORE` must be a comma list of `bundled` / `system`. The client
  certificate, its private key and the passphrase are carried from the parent and cannot be
  set by an override (`override_denied`); for a Codex child the certificate name is not on
  the allowlist at all (`override_not_allowed`).
- `CLAUDE_CODE_SIMPLE` is on the Claude scrub list by name (PRODUCT.md d84cdd26 section 3.3), so
  it never reaches a Claude Code child and an override naming it is refused.
- The user's own `CLAUDE_CODE_OAUTH_TOKEN` reaches a Claude Code child unchanged when the
  parent has it (PRODUCT.md d84cdd26 section 3.3, Nov 8 fleet correction: nothing removes a
  built-in sign-in method from every run). This passthrough is accepted (owner call). What
  holds in every auth mode:
  - absent in the parent means absent in the child;
  - an override naming it is refused (`override_denied`);
  - a Codex child never gets it;
  - this package never sets, stores, checks or logs it, and `ChildRoute` has no field for it.
- A Claude Code child may update its own binary. This is intended (owner call):
  `DISABLE_AUTOUPDATER` is not on the Claude allowlist and the core does not set it, so the
  parent's value does not reach the child. An override naming `DISABLE_AUTOUPDATER` is still
  refused (`override_not_allowed`).
- `launch.spawn(argv, validated_env, ...)` is the launch boundary. It accepts only a
  `ValidatedEnv`, always passes it as Popen's explicit `env=`, resolves argv[0] on the
  validated PATH, and refuses caller options that would replace or widen the launch.
  The child always gets its own session: `start_new_session` other than `True` and
  `process_group` are refused.
- A temp name (`TMPDIR`, `TMP`, `TEMP`) is replaced by one private 0700 route temp root
  per process. It is kept only if its whole resolved directory chain passes the
  owned-chain rule, so a daemon `TMPDIR` that another user can write is skipped for
  `/tmp`; with no safe base the build refuses with `TMPDIR` / `tmp_root_unsafe`.
- `HOME` must be our own 0700 directory and not itself a link. A trailing slash or `/.`
  is the same entry, so a link spelled that way is still refused. A home path ending in
  `..` is refused, since `link/sub/..` reaches the link's target. Parents are checked
  on the resolved path, so an accepted link above it (macOS `/var`) does not refuse it.
- A path value is resolved one component at a time. A directory that a `..` steps back out
  of, in the value or in a link's target, must pass the owned-chain rule too, although it
  is not part of the resolved path: `pub/hop/../../ca.pem` with `pub` open to others
  refuses with `value_chain_unsafe`, since `hop` could be swapped for a link after the
  check. A `..` out of directories only root or we can write is accepted.
- The old route transport (`scripts/v3/route_contract/transport.py`) is frozen and
  unchanged; it still passes `os.environ.copy()`.

POSIX only. On Windows, and wherever `os.geteuid`, `os.getuid` or the `fcntl`
module is missing, every call refuses with `platform_unsupported` instead of
passing values the ownership rules cannot judge or raising `AttributeError`.
A Windows SID/DACL backend is future work. Until then the directory-owner,
symlink-owner and mode `0o077` checks run only where `os.geteuid` exists. Where
it does not, they refuse (`owner_check_unsupported`, or the owned-chain and
private-dir checks return false). They never return true and never invent a uid.

## Auth mode and the route

Decided by Lucas and implemented here (CSLR-D-011; PRODUCT.md 055cc7a5 section 3.3, "Every
route starts with an explicit auth mode").

`auth_mode` is required and is exactly one of `subscription`, `api_key`, `cloud_provider`
(`policy.AUTH_MODES`). Omitted refuses with `auth_mode_missing`; anything else refuses with
`auth_mode_unknown`. No mode is a default and none is read from the launching shell.

`route=ChildRoute(api_key=None, base_url=None, provider_switches=(), trusted_base_urls=(),
full_access_acknowledged=False)` is what the caller says about the route it launched. It is a
separate input from the parent environment and from `overrides`. The override path is
unchanged: it still refuses every key, token, base URL and provider-switch name
(`override_denied`).

In every mode the parent's vendor keys, `ANTHROPIC_BASE_URL` and the Bedrock, Vertex and
Foundry switches are dropped. The child gets one only from the route:

| Mode | Claude Code child | Refuses |
| --- | --- | --- |
| `subscription` | No key. The OAuth passthrough above is kept. | A route with a key (`route_key_not_allowed`) or a switch (`route_provider_not_allowed`), and a base URL the user has not trusted (`route_base_url_untrusted`, see [below](#subscription-base-url)). |
| `api_key` | `ANTHROPIC_API_KEY` is the route's key, never the parent's. `CLAUDE_CODE_OAUTH_TOKEN` stays if the parent has it; the key outranks it (section 3.3). | No key (`route_key_missing`), an empty or malformed key (`route_key_malformed`), a switch (`route_provider_not_allowed`). |
| `cloud_provider` | Each switch the route names (`CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY`) is set to `1`. A switch left in the parent is not a pick. | No switch (`route_provider_missing`), another name (`route_provider_unknown`), a key (`route_key_not_allowed`). |

- **`ANTHROPIC_BASE_URL` is route-owned, not inherited.** The parent's value is dropped in
  every mode. The child gets `ANTHROPIC_BASE_URL` only when the route supplies `base_url` (a
  proxy, an OpenRouter-style gateway, another Anthropic-compatible base); with none it is
  unset. The supplied URL must be `http(s)://host[:port][/path]`: a user name or password
  refuses with `base_url_userinfo`, and a missing or other scheme, a query, a fragment, a bad
  port or a `..` path refuses with `base_url_malformed`. A bracketed host must be an IPv6
  address (`[::1]`, `[2001:db8::1]`): `[:::]` or `[1:2:3]` refuses with `base_url_malformed`,
  and the same host in a proxy value refuses with `proxy_malformed`. On an `api_key` or `cloud_provider`
  route a URL that meets this rule is set as it is. On a `subscription` route it must also
  pass the rule in [Subscription base URL](#subscription-base-url).
- **`NO_PROXY` includes the route host.** While a proxy is set (`HTTP_PROXY`, `HTTPS_PROXY`,
  `ALL_PROXY`, or the lowercase names), both `NO_PROXY` and `no_proxy` are checked against the
  route base host as well as the engine's own endpoints. Either name is enough to refuse.
  `NO_PROXY=gateway.invalid` cannot send `https://gateway.invalid` around the proxy
  (`no_proxy_engine_host`). A CIDR matches an IP it contains (`10.0.0.0/8` covers `10.1.2.3`,
  `::1/128` covers `::1`), including a requests shorthand (`10.0/8` via `socket.inet_aton`),
  a permissive prefix (`10.0.0.0/+8`, `10.0.0.0/\t8` from requests, `10.0.0.0/8x` from curl;
  curl also ignores whatever follows those digits, so `10.0.0.0/8/foo` and `10.0.0.0/8/24` are `/8`;
  leading zeros do not change the value, so `10.0.0.0/00008/foo` is `/8`. requests' `int()`
  of the whole suffix is also read, so `10.0.0.0/0_8`, `10.0.0.0/+0_8` and a fullwidth `8`
  are `/8`. A zero with junk that `int()` rejects is not a prefix: `10.0.0.0/0x8/foo` is not `/0`),
  the IPv4 address inside `[::ffff:10.1.2.3]`, and an IPv4-mapped CIDR compared as IPv4
  (Go: `::ffff:10.0.0.0/104` is `10.0.0.0/8`, `::ffff:0:0/96` is every IPv4). `::/0` stays IPv6.
  A prefix of 0 covers that whole address family (`0.0.0.0/0`, `::/0`); a bare `/0` covers every
  IP of both families. A CIDR does not match a domain name. A prefix that cannot be read without
  raising (`10/²`, a thousands-digit mask) refuses with `no_proxy_malformed`. A NUL in the value
  refuses with `value_malformed`. A bracketed IPv6 entry (`[::1]`) is that address. IDNA2003 and
  IDNA2008 spellings both match (`bücher.invalid`, `faß.invalid` and `xn--fa-hia.invalid`,
  `FAẞ.INVALID`, a trailing dot). Before that, names are mapped with the Unicode 15.1.0
  UTS46 table, nontransitional (deviation characters stay: `ß` stays `ß`, final sigma stays
  final sigma). Ignored characters include U+180F. Cherokee U+13A0 stays U+13A0 (`xn--58d`);
  U+1C80 maps to U+0432 (`xn--b1a`). `faß。invalid`, `ｆａß.invalid`, `fªß.invalid`
  and `fa` plus a variation selector match `xn--fa-hia.invalid`. An entry that maps to empty or
  only dots (`\u00ad`, `。`, and a literal `.` or `.:443` against a trailing-dot host) refuses.
  A code point the 15.1 table does not map, ignore, or accept refuses too: IDNA2003 is not
  a fallback, because it would rewrite `ß` to `ss` and miss the name a newer Go lookup uses.
  Trailing dots are stripped again after that mapping. Before that strip, requests' suffix
  test is applied to the hostname and to `hostname:port` (engine endpoints are HTTPS, port
  443; a route uses its own origin port). `:443`, `443`, `43` and `3` therefore match
  `https://gateway.invalid`.
  A leading `*.` is normalised the same way, so `*.bücher.invalid` matches
  `sub.xn--bcher-kva.invalid`. Dropping an ignored character does not turn `.*` into a
  match-all. Final sigma
  (`ς`) is not small sigma (`σ`). A leading dot is suffix syntax, so `.münchen.example` does not
  match an unrelated host. A non-ASCII entry that will not encode refuses. Undici 7.30.1's leading
  `*.` matches the apex, so `*.gateway.invalid` and `*.gateway.invalid:443` match `gateway.invalid`.
  With no proxy set, `NO_PROXY` is kept as it is.
  The match is the union of the readers, and it over-refuses on purpose:
  - ports are dropped, so `gateway.invalid:9` matches `gateway.invalid`;
  - a `*` in the middle matches across dots, so `gate*invalid` matches `gateway.invalid`;
  - an entry that is only `*`, `.`, or both (`*`, `*.`, `**`, `.*`) is `no_proxy_wildcard`;
  - a scheme is stripped, so `http://gateway.invalid` is `gateway.invalid`;
  - spaces are deleted the way requests does, so `gate way.invalid` is `gateway.invalid`;
  - a plain suffix needs no dot boundary. That is requests <=2.32.3 (`hostname.endswith(entry)`),
    and this check follows it because a child may still run that requests. `ateway.invalid`
    therefore exempts `gateway.invalid`. A minimum requests version is not assumed.
  `corp.internal`, `localhost`, `127.0.0.1` and `::1` do not match a remote name such as
  `gateway.invalid`, and the list is kept.
  `npm_config_no_proxy`, `NPM_CONFIG_NO_PROXY` and `GLOBAL_AGENT_NO_PROXY` are not on the
  allowlist, so they are dropped and are not read as bypass lists.
- **Key custody.** This package is the launch boundary only. It never reads the OS keyring or
  the owner-only key file (A44), and does no keyring or file I/O. The caller fetches the
  user-entered key from the OS keyring, or from the owner-only file on a host with no
  keyring, and passes it as `ChildRoute(api_key=...)` on an `api_key` route only. The key is
  never in `repr` or `str` of the route, in a refusal, or in `repr` of the `ValidatedEnv`.
  CSL holds no credential (A28).
- **Codex child.** It never gets `CLAUDE_CODE_OAUTH_TOKEN`. `subscription` is the behaviour
  before this change: no vendor name reaches it. `api_key` sets `CODEX_API_KEY` (the variable
  `codex exec` reads) to the route's key; the parent's `CODEX_API_KEY`, `OPENAI_API_KEY` and
  `OPENAI_BASE_URL` are dropped in every mode. A route `base_url` refuses with
  `route_base_url_unsupported`, and `cloud_provider` refuses with `auth_mode_unsupported`:
  the Codex profile has no base-URL or cloud-provider route here.
- A route that is not a `ChildRoute`, or whose switches are not a tuple, list or frozenset
  of names, refuses with `route_malformed`.

### Subscription base URL

Decided by Lucas (CSLR-D-011; PRODUCT.md `055cc7a5c793b7135e5d0565dc4198cf96230531`, SHA-256
`e64783152f6d67d4ede774dc41d112fe46c2d7536d53cb2c04e71a3566044daf`, section 3.3 and the
full-access passages Q20, O44 and H-06): refuse a subscription base URL by default, allow
official Anthropic without a prompt, trust a named endpoint once and remember it, and let the
named full-access profile skip the check with its danger notice.

A `subscription` route with `base_url` set refuses with `ANTHROPIC_BASE_URL` /
`route_base_url_untrusted` by default. The reason: the accepted `CLAUDE_CODE_OAUTH_TOKEN`
passthrough would go to that base. It launches in exactly four cases:

1. **No base URL.** The child uses the engine's own default, as before.
2. **Official Anthropic.** The base URL's origin is exactly `https://api.anthropic.com`. No
   prompt and no trusted entry is needed.
3. **Trusted by the user.** The route's `trusted_base_urls` holds the base URL's origin.
4. **Named full-access profile.** The route's `full_access_acknowledged` is `True`.

The inputs, both on `ChildRoute`:

- `trusted_base_urls: tuple[str, ...] = ()` holds the origins the user already agreed to send
  their subscription sign-in to. It is a tuple or frozenset of `scheme://host[:port]` strings.
  Each entry must meet the base URL rule above and name an origin only (no path beyond `/`).
  Any other container or entry refuses the whole route with
  `route_trusted_base_urls_malformed`. A list that holds a different origin does not help:
  the route still refuses with `route_base_url_untrusted`.
- `full_access_acknowledged: bool = False` means this run uses the named full-access profile
  and the user acknowledged it for this run (CSL-460's full-access mode; PRODUCT.md 055cc7a5
  Q20, O44, H-06). Only the bool `True` counts. Any value that is not a bool (`"true"`, `1`,
  `None`) refuses with `route_full_access_malformed`. With it set, a subscription base URL
  launches without the trust check; the base URL value rule (`base_url_malformed`,
  `base_url_userinfo`) still applies.

How origins are compared. The base URL value rule runs first. Then the origin is the scheme,
host and port, and two origins match only when all three are equal. Nothing is matched by
prefix or substring.

- Scheme and host are lowercased, so `HTTPS://API.Anthropic.COM` is the official origin.
- A missing port is the scheme's default (443 for `https`, 80 for `http`), so
  `https://api.anthropic.com:443` is the official origin and `https://api.anthropic.com:8443`
  is not.
- The path is not part of the origin. `https://api.anthropic.com/v1` is official, and a
  trusted origin covers every path on it.
- A trailing dot is kept: `https://api.anthropic.com.` is a different origin, is not
  official, and needs its own trusted entry.
- IP addresses are compared as written. `https://[::1]` and `https://[0:0:0:0:0:0:0:1]` are
  different origins.
- These refuse as untrusted: `http://api.anthropic.com`, `https://api.anthropic.com:8443`,
  `https://api.anthropic.com.evil`, `https://api.anthropic.com.evil.com`. A URL with a user
  name, such as `https://user@api.anthropic.com`, refuses earlier with `base_url_userinfo`,
  whatever is trusted.

What this package does not do:

- The one-time prompt and the remembered trusted list live at the driver layer, not in
  `child_env`. This package reads and writes no storage for them, reads no environment
  variable for them, and never prompts. The driver asks the user once, saves the origin in
  the user's config, and passes the saved origins in `trusted_base_urls` on later runs.
- That prompt must warn the user that their subscription token goes to that endpoint.
- The driver must show the danger notice when the named full-access profile is turned on.
  `child_env` renders no UI.
- Full access is never inferred from the parent environment or from an override. Only the
  route's `full_access_acknowledged=True` counts.

Other modes and engines:

- `api_key` and `cloud_provider` routes are unchanged. A custom base URL still sets
  `ANTHROPIC_BASE_URL` with no trust needed. Well-formed `trusted_base_urls` and
  `full_access_acknowledged` values are ignored there and change nothing. Malformed ones
  refuse in every mode, like any other malformed route.
- A Codex child is unchanged. A route `base_url` still refuses with
  `route_base_url_unsupported`, whatever is trusted or acknowledged.
- `repr` and `str` of a `ChildRoute` show a count for `trusted_base_urls` and `yes` or `no`
  for `full_access_acknowledged`. No URL appears there or in any refusal.

Not in this package:

- The `apiKeyHelper` prompt in section 3.3 (show the user that a subscription run would bill
  their API key and let them pick) belongs to the driver and launch layer. An `api_key` route
  here needs the key itself.
- Cloud-provider credentials and region names (for example `AWS_*`) are not on the
  allowlist. A `cloud_provider` route carries the switch and nothing else from the parent.
- CX-950 (whether per-run changes for the picked route restrict a built-in method) stays
  open. Nothing here decides it.

## Tests

From the repository root (Python 3.11+, standard library only):

```sh
PYTHONPATH=core python3 -B -m unittest discover -s core/recursion_core/child_env/tests -t core -v
PYTHONPATH=core python3 -B -m recursion_core.child_env.tests.cells 5
```

The second command prints one JSON row per cell (n trials each) and a per-group summary.

The cells build their own directory chain and never put a host directory on `PATH`. Run
42733662905 (`Linux tests`, ubuntu-24.04 image 20261101.360) failed three cells that named
the host's `/usr/local/bin` and scanned the host's `/usr/bin`: the image makes
`/usr/local/bin` mode 0777 (`install-nodejs.sh`, `chmod -R 777 /usr/local/bin`) and
`/usr/share` 0777 (`configure-system.sh`), with `/usr/bin` links into it. The rules refuse
both, correctly. The fixtures now use their own `usr/bin`, `bin`, `usr/local/bin` and
`usr/sbin` under the scratch root, the scratch root is made where its whole chain is owned,
and the `hosted` cells keep the runner layout as a refusal. A real child launched on such a
host with `/usr/local/bin` on `PATH` is refused (`PATH` / `value_chain_unsafe`).
`tools/ci_gate.py` discovers `core/**/test_*.py` and puts `core/` on `PYTHONPATH` for
those tests (CSL-795). The commands above run this package on its own.

## Evidence

Rules and cells come from candidate csl-research records (links only; the research stays there):

- `submissions/20261102-csl678-value-check-v3-01` (C2-37, rule N5h)
- `submissions/20261103-csl678-envpath-path-noproxy-01` (C2-38, rule N5i)
- `submissions/20261107-csl785-child-env-allowlist-01` (this change's record)
