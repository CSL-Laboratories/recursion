# routes: one route contract, Claude Code and Codex adapters, connection-keyed breaker (CSL-797)

Candidate code for build item 7. Spec: PRODUCT.md at csl-research `398cd812cb8a9cf0ef3d1950f1b15bd9a76e3dfa`
(SHA-256 `36666beeadacb2af741db0ee55022b2e3911513b4de329ec500d50badbb3a226`): 3.4 S3 (breaker), S4 (hung
engine), 11.2 (breaker, plan limits), Q12 (Claude subscription only inside Claude Code).

## What it is

- `contract.py`: the route contract. Requests, results, events and limit signals are plain dicts that
  round-trip through JSON; every refusal or failure has a typed code (`CODES`). Surfaces and the daemon
  call `RouteService.handle(request) -> result` and nothing else, so the tests drive behaviour through
  the protocol and a later port can reuse them.
- `claude_code.py`: `ClaudeCodeAdapter`, `claude -p --input-format stream-json --output-format stream-json`.
  Reads typed limit signals only: `rate_limit_event`, `api_error_params.rate_limit_info` (also on
  `system`/`api_retry`, which is surfaced as a typed `retry` event), and a typed `error: "rate_limit"`.
- `codex.py`: `CodexAdapter`, `codex app-server` (JSON-RPC over JSONL on stdio). Reads
  `account/rateLimits/read`, `account/rateLimits/updated` and typed JSON-RPC / turn errors.
- `breaker.py`: `ConnectionBreaker`, keyed by the connection a route is billed through
  (`subscription:<login>` or `api_key:<key id>`), per model only when the typed signal names a per-model
  vendor bucket. Never keyed by engine id or tenant.
- `profile_route.py`: Pi, Hermes and OpenCode routes; every launch goes through
  `recursion_core.subscription_routes.guard_then_spawn` (B3, CSL-786), so a Claude-subscription provider is
  refused as `subscription_route_refused` before the launcher is called.
- `launch.py`: the injected launcher / process interface, `require_free_threaded()` (refuses a GIL build;
  the daemon calls it before starting processes and after each plugin load) and `child_env()` (explicit
  env from an allowlist; never `os.environ`).

## Rules it enforces

- Only a typed limit signal opens a connection; it fences every tenant on that connection and no other
  connection. Text inside the model's payload (assistant/user content, Codex agent messages) is never
  inspected. Vague quota-sounding error text is a `short_fault`: one model, one tenant, 60 s (grill Q23).
- Recursion's own deadline is `deadline_exceeded`: the run is fenced (`EngineProcess.fence`) for that
  request only and the breaker is not told. Other engine failures are request-local too.
- Recovery: an open key always has a finite end (vendor `reset_at`, capped; otherwise a 300 s cooldown
  doubling to 3600 s). Then one leased half-open probe at a time (150 s lease); success closes, a limit
  reopens, a lease that expires lets the next request probe; `reset()` closes at once. These numbers are
  uncalibrated defaults (11.2, B-10).
- B11 (CSL-783 F70, CSL-727 F19): the Codex stdio worker is launched as
  `codex -c check_for_update_on_startup=false app-server` and the adapter starts nothing else; a test runs
  48 turns over 72 fake minutes and checks every spawn. `daemon_config()` holds
  `updater.autoUpdateEnabled = false` and `check_daemon_config()` refuses anything else.
- Child env: Codex never inherits `CODEX_API_KEY`, `CODEX_ACCESS_TOKEN` or `OPENAI_API_KEY`; a Codex
  subscription run uses Codex's own CLI login under `CODEX_HOME`, and Recursion holds no ChatGPT token.
  An `api_key` route gets back only the one key it selects, from the injected credential lookup. The full
  child-env rules (B1, CSL-785, #320) are a separate module.

## Claude Code driver (Lucas's correction, Nov 7 ~6:34pm PST)

This replaces the Nov 8 briefing sentence "Refuse --bare. Clear apiKeyHelper, CLAUDE_CODE_OAUTH_TOKEN and
CLAUDE_CODE_SIMPLE from the engine environment." Each run uses the route the user picked, and only that
run's environment changes:

- `subscription`: no `--bare`. Dropped for the run: `CLAUDE_CODE_SIMPLE`, `ANTHROPIC_API_KEY`,
  `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_PROFILE`, `CLAUDE_CODE_USE_BEDROCK` / `_VERTEX` / `_FOUNDRY`; any
  other unlisted name (including federation variables, whose names are not in repository evidence) is
  dropped by the allowlist. The user's own `CLAUDE_CODE_OAUTH_TOKEN` and `CLAUDE_CONFIG_DIR` pass through
  untouched; `/login` is the engine's. If the user's settings carry an `apiKeyHelper` (reported by the
  surface as `route.api_key_helper`; this adapter never opens settings), the result is
  `route_choice_required`: the run would bill their API key, and they pick (`billing_ack`, the API-key
  route, or cancel). Not reported is the same question.
- `--bare` as the `-p` default (B20, CSL-726): Anthropic's headless docs say `--bare` "will become the
  default for `-p` in a future release", and under `--bare` OAuth and the keychain are never read. No
  opt-out flag is in repository evidence, so none is passed. Instead the surface reports
  `route.bare_default` from `bare_default.probe()` (one `claude --help`, small fixed env, 10 s timeout):
  `absent` launches; `present`, `unknown` or not reported is `launch_refused` before anything spawns, and
  never trips the login's breaker. 2.1.312's help lists `--bare` with no default note, so it reads
  `absent`. The API-key and passthrough routes do not need the field.
- `api_key`: the user's own key from custody (`ANTHROPIC_API_KEY`) or their own `apiKeyHelper`
  (`key_source: "api_key_helper"`, no key added); `--bare` is allowed (`route.bare`). The user's
  `CLAUDE_CODE_OAUTH_TOKEN` still passes through; the key outranks it by the engine's own precedence.
- `passthrough` (cloud providers and any other built-in method the user picks): the env passes through
  unchanged for that run, and `--bare` is the user's choice there too. `--bare` is refused only on the
  subscription route. No route drops `CLAUDE_CODE_OAUTH_TOKEN`, and the parent env is never changed.
- The adapter never edits settings or credential files, never sets `DISABLE_LOGIN_COMMAND`, and never
  sets, stores or reads `CLAUDE_CODE_OAUTH_TOKEN`. The binary is `claude` itself; its hash and version are
  the R2a launch check's (CSL-732). The Commercial Terms review of this env scrub is still open in
  csl-research.

## Limits (`limits.py`)

At a `plan_limit` or `breaker_open` result, the result carries a `limit_notice` (connection, engine,
model, scope, reset time, choices). `route.limits` or the service default sets `on_limit`: `ask` (default:
nothing else runs), `wait` (nothing else runs; re-submit at `reset_at`) or `fallback` (the user's own
`fallback` routes in the user's order, each switch shown as a `fallback` event and `switched_to`).
Nothing picks a route by itself; `fallback` with an empty list is refused.

Lesson: [CSL-608](https://linear.app/csl-laboratories/issue/CSL-608) (Done research) found master's breaker
keyed by engine, shared by every tenant and never reset. This module does not claim to have measured
anything; no real limit capture has gone through it.

## Candidate and unwired

- No daemon calls `RouteService` yet; no real launcher (cgroup v2 leaf + pidfd) is in this module.
- Vendor field and option names marked "unverified" in the code were not read from vendor source
  (Claude `rate_limit_info` inner fields, Codex method names beyond `account/rateLimits/*`,
  `codexErrorInfo` values, `check_for_update_on_startup`).
- Pi/Hermes/OpenCode stdio drivers are injected, not built here.
- No live provider call, real `claude`/`codex` process or network is used anywhere; tests use fakes only.

## Tests

From the repository root:

```
PYTHONPATH=core python3 -B -m unittest discover -s core/recursion_core/routes/tests -t core -v
PYTHONPATH=core python3.14t -X gil=0 -B -m unittest discover -s core/recursion_core/routes/tests -t core -v
```
