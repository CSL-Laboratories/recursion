# engine/child_env (CSL-785, candidate)

New-core child environment for engine children. Candidate code: not yet called by a
running core, and not reviewed or accepted.

- `policy.build_child_env(engine, home=..., parent=...)` builds the child environment
  from an exact-name allowlist per engine profile (`claude_code`, `codex`), checks the
  values, and returns a `ValidatedEnv` or raises `EnvRefused(name, cause)` before any
  spawn. Values are never included in errors or `repr`.
- `launch.spawn(argv, validated_env, ...)` is the launch boundary. It accepts only a
  `ValidatedEnv`, always passes it as Popen's explicit `env=`, resolves argv[0] on the
  validated PATH, and refuses caller options that would replace or widen the launch.
- The old route transport (`scripts/v3/route_contract/transport.py`) is frozen and
  unchanged; it still passes `os.environ.copy()`.

POSIX only. On Windows every call refuses with `platform_unsupported`.

## Tests

From the repository root (Python 3.11+, standard library only):

```sh
python3 -B -m unittest discover -s engine/child_env/tests -t . -v
python3 -B -m engine.child_env.tests.cells 5
```

The second command prints one JSON row per cell (n trials each) and a per-group summary.
`tools/ci_gate.py` does not discover `engine/` tests; they run only when started by hand.

## Evidence

Rules and cells come from candidate csl-research records (links only; the research stays there):

- `submissions/20261102-csl678-value-check-v3-01` (C2-37, rule N5h)
- `submissions/20261103-csl678-envpath-path-noproxy-01` (C2-38, rule N5i)
- `submissions/20261107-csl785-child-env-allowlist-01` (this change's record)
