# engine/subscription_routes (CSL-786, candidate)

Refuses Claude-subscription routes in Pi, Hermes and OpenCode engine profiles before any
spawn. Under Q12 the Claude subscription is used only inside Claude Code; other harnesses
use an Anthropic API key. Candidate code: not yet called by a running core, and not
reviewed or accepted.

- `check_route(engine, profile, child_env, route_home=..., parent_env=..., operator_home=...)`
  validates one `pi`, `hermes` or `opencode` profile and the exact child environment, and
  returns a `RouteDecision` or raises `SubscriptionRouteRefused(engine, cause, detail)`.
- `guard_then_spawn(launcher, argv, ...)` calls `check_route` and only then the launcher.

What refuses:

- any of `ANTHROPIC_OAUTH_TOKEN`, `ANTHROPIC_AUTH_TOKEN` (Pi), `ANTHROPIC_TOKEN`,
  `CLAUDE_CODE_OAUTH_TOKEN` (Hermes) in the child env, for every engine;
  `CLAUDE_CONFIG_DIR` in the child env;
- a Claude-subscription provider or auth kind (OpenCode's Claude Agent provider, Pi and
  Hermes Anthropic OAuth), and any provider, auth kind or credential variable not on the
  engine's explicit API-key list (fail closed);
- stored-credential adoption: the child's HOME must be the route home, private, not the
  operator's home, and must hold no Claude Code store entry (`.claude`, `.claude.json`,
  `.config/claude`). Checked with `lstat` only; no credential file is opened.

A subscription variable present only in the parent env is flagged in `RouteDecision.flags`.
API-key routes (`anthropic`, `openai`) launch.

## Tests

From the repository root (Python 3.11+, standard library only):

```sh
python3 -B -m unittest discover -s engine/subscription_routes/tests -t . -v
```

`tools/ci_gate.py` does not discover `engine/` tests; they run only when started by hand.

## Evidence

Candidate csl-research records (links only; the research stays there):
`submissions/20261107-csl786-subscription-routes-01` (this change's record), and the CSL-712
grounding records on csl-research branch `grounding-1077` (engine CLIs V51 and V56; vendor
terms H1 to H3).
