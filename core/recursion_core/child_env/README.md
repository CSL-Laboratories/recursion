# core/recursion_core/child_env (CSL-785, candidate)

New-core child environment for engine children. Candidate code: not yet called by a
running core, and not reviewed or accepted.

- `policy.build_child_env(engine, home=..., parent=...)` builds the child environment
  from an exact-name allowlist per engine profile (`claude_code`, `codex`), checks the
  values, and returns a `ValidatedEnv` or raises `EnvRefused(name, cause)` before any
  spawn. Values are never included in errors or `repr`. `overrides=` may only set a name
  already on the engine's allowlist (never `HOME`, `PATH` or a credential name); overrides
  are merged first and then meet exactly the same value, PATH, NO_PROXY and temp rules.
- Allowlist: the 29 names the Nov 5 tree carried plus `ALL_PROXY`, `all_proxy` and
  `XDG_RUNTIME_DIR` (32 names, both engines), and for Claude Code also
  `CLAUDE_CODE_CLIENT_CERT`, `CLAUDE_CODE_CLIENT_KEY`, `CLAUDE_CODE_CLIENT_KEY_PASSPHRASE` and
  `CLAUDE_CODE_CERT_STORE` (PRODUCT.md 055cc7a5 section 3.8, C1-46). `ALL_PROXY` meets the proxy
  rule with SOCKS schemes allowed and counts as a set proxy for the `NO_PROXY` rule; the client
  cert and key meet the file rule; `XDG_RUNTIME_DIR` meets the directory rule;
  `CLAUDE_CODE_CERT_STORE` must be a comma list of `bundled` / `system`. The passphrase is
  carried from the parent but, like every secret-shaped name, cannot be set by an override.
- The user's own `CLAUDE_CODE_OAUTH_TOKEN` reaches a Claude Code child unchanged when the
  parent has it, and is absent when the parent does not (PRODUCT.md 055cc7a5 section 3.3,
  Nov 8 fleet correction: nothing removes a built-in sign-in method from every run). The
  module does not check, log or set it, and an override naming it is refused
  (`override_denied`). A Codex child never gets it.
- `launch.spawn(argv, validated_env, ...)` is the launch boundary. It accepts only a
  `ValidatedEnv`, always passes it as Popen's explicit `env=`, resolves argv[0] on the
  validated PATH, and refuses caller options that would replace or widen the launch.
  The child always gets its own session: `start_new_session` other than `True` and
  `process_group` are refused.
- A temp name (`TMPDIR`, `TMP`, `TEMP`) is replaced by one private 0700 route temp root
  per process. It is kept only if its whole resolved directory chain passes the
  owned-chain rule, so a daemon `TMPDIR` that another user can write is skipped for
  `/tmp`; with no safe base the build refuses with `TMPDIR` / `tmp_root_unsafe`.
- `HOME` must be our own 0700 directory and not itself a link; its parents are checked
  on the resolved path, so an accepted link above it (macOS `/var`) does not refuse it.
- The old route transport (`scripts/v3/route_contract/transport.py`) is frozen and
  unchanged; it still passes `os.environ.copy()`.

POSIX only. On Windows every call refuses with `platform_unsupported`.

## Open (Lucas's call)

- There is no per-route auth mode yet (PRODUCT.md 055cc7a5 section 3.3 "Every route starts
  with an explicit auth mode"; CX-950 open). The API-key and cloud-provider scrub lists
  apply to every run, so an API-key or Bedrock/Vertex/Foundry route cannot be launched
  through this boundary, and the `ANTHROPIC_BASE_URL` question the spec leaves open is
  settled here as "dropped". How a user-entered key gets from local secure storage into a
  child (A28, A44) is a product choice; this module does not pick it.

## Tests

From the repository root (Python 3.11+, standard library only):

```sh
PYTHONPATH=core python3 -B -m unittest discover -s core/recursion_core/child_env/tests -t core -v
PYTHONPATH=core python3 -B -m recursion_core.child_env.tests.cells 5
```

The second command prints one JSON row per cell (n trials each) and a per-group summary.
`tools/ci_gate.py` does not discover `core/` tests; they run only when started by hand.

## Evidence

Rules and cells come from candidate csl-research records (links only; the research stays there):

- `submissions/20261102-csl678-value-check-v3-01` (C2-37, rule N5h)
- `submissions/20261103-csl678-envpath-path-noproxy-01` (C2-38, rule N5i)
- `submissions/20261107-csl785-child-env-allowlist-01` (this change's record)
