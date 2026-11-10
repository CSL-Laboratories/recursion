# core/recursion_core/child_env (CSL-785, candidate)

New-core child environment for engine children. Candidate code: not yet called by a
running core, and not reviewed or accepted.

Spec: CSLR-D-011, PRODUCT.md `055cc7a5c793b7135e5d0565dc4198cf96230531` (SHA-256
`e64783152f6d67d4ede774dc41d112fe46c2d7536d53cb2c04e71a3566044daf`), section 3.3.

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
- `CLAUDE_CODE_SIMPLE` is on the Claude scrub list by name (PRODUCT.md 055cc7a5 section 3.3), so
  it never reaches a Claude Code child and an override naming it is refused.
- The user's own `CLAUDE_CODE_OAUTH_TOKEN` reaches a Claude Code child unchanged when the
  parent has it (PRODUCT.md 055cc7a5 section 3.3, Nov 8 fleet correction: nothing removes a
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
- The old route transport (`scripts/v3/route_contract/transport.py`) is frozen and
  unchanged; it still passes `os.environ.copy()`.

POSIX only. On Windows every call refuses with `platform_unsupported`.

## Auth mode and the route

Decided by Lucas and implemented here (CSLR-D-011; PRODUCT.md 055cc7a5 section 3.3, "Every
route starts with an explicit auth mode").

`auth_mode` is required and is exactly one of `subscription`, `api_key`, `cloud_provider`
(`policy.AUTH_MODES`). Omitted refuses with `auth_mode_missing`; anything else refuses with
`auth_mode_unknown`. No mode is a default and none is read from the launching shell.

`route=ChildRoute(api_key=None, base_url=None, provider_switches=())` is what the caller says
about the route it launched. It is a separate input from the parent environment and from
`overrides`. The override path is unchanged: it still refuses every key, token, base URL and
provider-switch name (`override_denied`).

In every mode the parent's vendor keys, `ANTHROPIC_BASE_URL` and the Bedrock, Vertex and
Foundry switches are dropped. The child gets one only from the route:

| Mode | Claude Code child | Refuses |
| --- | --- | --- |
| `subscription` | No key. The OAuth passthrough above is kept. | A route with a key (`route_key_not_allowed`) or a switch (`route_provider_not_allowed`). |
| `api_key` | `ANTHROPIC_API_KEY` is the route's key, never the parent's. `CLAUDE_CODE_OAUTH_TOKEN` stays if the parent has it; the key outranks it (section 3.3). | No key (`route_key_missing`), an empty or malformed key (`route_key_malformed`), a switch (`route_provider_not_allowed`). |
| `cloud_provider` | Each switch the route names (`CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY`) is set to `1`. A switch left in the parent is not a pick. | No switch (`route_provider_missing`), another name (`route_provider_unknown`), a key (`route_key_not_allowed`). |

- **`ANTHROPIC_BASE_URL` is route-owned, not inherited.** The parent's value is dropped in
  every mode. The child gets `ANTHROPIC_BASE_URL` only when the route supplies `base_url` (a
  proxy, an OpenRouter-style gateway, another Anthropic-compatible base); with none it is
  unset. The supplied URL must be `http(s)://host[:port][/path]`: a user name or password
  refuses with `base_url_userinfo`, and a missing or other scheme, a query, a fragment, a bad
  port or a `..` path refuses with `base_url_malformed`.
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
