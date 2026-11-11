# routes: one route contract, Claude Code and Codex adapters, connection-keyed breaker (CSL-797)

Candidate code for build item 7. Spec: PRODUCT.md at csl-research `055cc7a5c793b7135e5d0565dc4198cf96230531`
(SHA-256 `e64783152f6d67d4ede774dc41d112fe46c2d7536d53cb2c04e71a3566044daf`, receipt pointer CSLR-D-011; first written
against `398cd812`, whose 3.4 S3/S4, 11.2 and Q12 text 055cc7a5 keeps): 3.4 S3 (breaker), S4 (hung
engine), 11.2 (breaker, plan limits), Q12 (Claude subscription only inside Claude Code).

## What it is

- `contract.py`: the route contract. Requests, results, events and limit signals are plain dicts that
  round-trip through JSON; every refusal or failure has a typed code (`CODES`). Surfaces and the daemon
  call `RouteService.handle(request) -> result` and nothing else, so the tests drive behaviour through
  the protocol and a later port can reuse them.
- `claude_code.py`: `ClaudeCodeAdapter`, `claude -p --input-format stream-json --output-format stream-json`.
  Reads typed limit signals only: `rate_limit_event` with `status: rejected`,
  `api_error_params.rate_limit_info` on an error `result`, and a typed `error: "rate_limit"`.
  `system`/`api_retry` is a `retry` event and does not open the breaker. `seven_day_opus` and
  `seven_day_sonnet` fence that model family; other `rateLimitType` values fence the connection.
- `codex.py`: `CodexAdapter`, `codex app-server` (JSON-RPC over JSONL on stdio). Reads
  `account/rateLimits/read`, `account/rateLimits/updated` and typed JSON-RPC / turn errors.
  A named `limitId` is the breaker key. An `api_key` route ignores a ChatGPT-auth error from the
  rate-limit read and still runs the turn.
- `breaker.py`: `ConnectionBreaker`. An API key's slot is `api_key:<family>:<env>:<id>`
  (`contract.breaker_key`): the same Anthropic custody lookup shares a meter across Claude Code and
  OpenCode, and a Codex OpenAI key with the same id does not. A subscription login stays
  `engine:kind:id`, so the same label on Claude and on Codex is not one login. Per model only when
  the typed signal names a per-model vendor bucket. Never keyed by tenant, and never by engine id alone.
- `profile_route.py`: Pi, Hermes and OpenCode routes; every launch goes through
  `recursion_core.subscription_routes.guard_then_spawn` (B3, CSL-786), so a Claude-subscription provider is
  refused as `subscription_route_refused` before the launcher is called. Pi and Hermes also pass
  `route.wrapper` (the measured launcher and package pin); without it the launch is
  `wrapper_pin_missing`. The launcher receives the env `check_route` accepted.
- `launch.py`: the injected launcher / process interface, `require_free_threaded()` (refuses a GIL build;
  the daemon calls it before starting processes and after each plugin load) and `child_env()` (explicit
  env from an allowlist; never `os.environ`).

## Rules it enforces

- Only a typed limit signal opens a connection; it fences every tenant on that connection and no other
  connection. Text inside the model's payload (assistant/user content, Codex agent messages) is never
  inspected. Vague quota-sounding error text is a `short_fault`: one model, one tenant, 60 s (grill Q23).
  The refusal while that fence holds is `short_fault` too, never `breaker_open`, so it gets no
  `limit_notice` and never triggers a fallback. Open and half-open keys are checked before it, so a
  short fault never hides a real limit on the same connection from that tenant.
- Recursion's own deadline is `deadline_exceeded`: the run is fenced (`EngineProcess.fence`) for that
  request only and the breaker is not told. Other engine failures are request-local too.
- Recovery: an open key always has a finite end (`reset_at` on the breaker clock, capped; otherwise a
  300 s cooldown doubling to 3600 s). Another typed limit while that window still holds is the same
  incident: it does not stack the cooldown. A probe that runs after the window and hits a limit again
  is the next incident. A probe token is booked once; a late record for a token that is no longer
  current does not change the key. Vendor `resetsAt` is Unix epoch and is translated onto that clock
  (one sample of each clock) before it is stored, so a monotonic deadline clock does not turn one
  limit into an 8-day fence.
  Then one leased half-open probe at a time (lease = the time still left on the request's deadline
  plus 30 s, 11.2 C1-18; 150 s only when no deadline is given); success closes, a limit
  reopens, a lease that expires lets the next request probe; `reset()` closes at once. These numbers are
  uncalibrated defaults (11.2, B-10).
- B11 (CSL-783 F70, CSL-727 F19): the Codex stdio worker is launched as
  `codex -c check_for_update_on_startup=false app-server` and the adapter starts nothing else; a test runs
  48 turns over 72 fake minutes and checks every spawn the adapter makes. That test cannot see what a
  real `codex` process spawns itself; the override's key name was read in Codex source (below), and
  a process-tree check on a real binary is still open. `daemon_config()` holds
  `updater.autoUpdateEnabled = false` and `check_daemon_config()` refuses anything else.
- Child env: Codex never inherits `CODEX_API_KEY`, `CODEX_ACCESS_TOKEN` or `OPENAI_API_KEY`; a Codex
  subscription run uses Codex's own CLI login under `CODEX_HOME`, and Recursion holds no ChatGPT token.
  An `api_key` route gets back only the one key it selects (`CODEX_API_KEY` or `OPENAI_API_KEY`; the
  `CODEX_ACCESS_TOKEN` sign-in token is never selectable), from the injected credential lookup. A
  Pi/Hermes/OpenCode `api_key` route selects only a name `subscription_routes` lists for that engine and
  the profile names, and refuses (`launch_refused`) when custody holds no key for it. The full
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
- `api_key`: the user's own key from custody (`ANTHROPIC_API_KEY`) or their own `apiKeyHelper`
  (`key_source: "api_key_helper"`, no key added); `--bare` is allowed (`route.bare`). The user's
  `CLAUDE_CODE_OAUTH_TOKEN` still passes through; the key outranks it by the engine's own precedence.
- `passthrough` (a cloud provider or gateway the user picks, `connection.method`: `bedrock`, `vertex`,
  `foundry` or `gateway`): that method's own variables (names from Claude Code's public docs) pass through
  unchanged for that run, with the base allowlist and the user's sign-in; nothing else from the daemon's
  environment does (no other engine's key, no stray `ANTHROPIC_API_KEY`). A method with no listed
  variables is `launch_refused`, and so is a method whose own switch or credential is unset
  (`CLAUDE_CODE_USE_BEDROCK` / `_VERTEX` / `_FOUNDRY`; for `gateway` both `ANTHROPIC_BASE_URL` and
  `ANTHROPIC_AUTH_TOKEN`): without it the run would fall back to the sign-in and bill the subscription.
  A `CLAUDE_CODE_USE_*` switch counts as set only for `1`, `true`, `yes` or `on`; `off`, `no`
  or anything else is refused. The child receives that spelling stripped and lowercased (Claude Code's
  bool parser accepts those four). These four methods are the whole passthrough contract; any other method
  is refused. Bedrock also passes the AWS SDK web-identity role variables (`AWS_ROLE_ARN`,
  `AWS_WEB_IDENTITY_TOKEN_FILE`, `AWS_ROLE_SESSION_NAME`). `--bare` is the user's choice there too. `--bare` is refused only on the
  subscription route. No route drops `CLAUDE_CODE_OAUTH_TOKEN`, and the parent env is never changed.
- The adapter never edits settings or credential files, never sets `DISABLE_LOGIN_COMMAND`, and never
  sets, stores or reads `CLAUDE_CODE_OAUTH_TOKEN`. The binary is `claude` itself; its hash and version are
  the R2a launch check's (CSL-732). The Commercial Terms review of this env scrub is still open in
  csl-research.

## Limits (`limits.py`)

At a `plan_limit`, `breaker_open` or `probe_in_flight` result, the result carries a `limit_notice` (connection, engine,
model, scope, reset time, choices). When the vendor sent no reset, `wait` uses the breaker's cooldown
end. `route.limits` or the service default sets `on_limit`: `ask` (default:
nothing else runs), `wait` (nothing else runs; re-submit at `reset_at`) or `fallback` (the user's own
`fallback` routes in the user's order, each switch shown as a `fallback` event and `switched_to`).
Every attempt shares the request's one deadline. Nothing picks a route by itself; `fallback` with an empty list is refused. A fallback entry may carry `wrapper` (the Pi/Hermes pin, as the measured object or the same fields in JSON). A request id that is already running is refused (`launch_refused`, detail `request in flight`) instead of launching a second child. `RouteService.shutdown()` waits for a launch that has already passed the closed check, fences every child it still has, and refuses later requests. A fence that fails stays tracked and the call raises, so a later shutdown retries it. Handles are tracked by identity, so the process object does not have to be hashable. Details and event fields are redacted when they contain a credential-shaped string (bearer tokens, JWTs, URL userinfo, JSON `\u` and `\/` escapes, and `%HH` percent-encoding). JSON and percent-encoding are peeled in turn, and every intermediate view is searched for secret tokens. Userinfo is one forward walk. Before a colon, any end-delimiter means there is no credential. A colon followed by an end-delimiter, or by digits (including `%34` and `\u0034`) and then an end-delimiter, is a port, not a password, so `https://example.com:443/search?q=user@host` and `https://example.com:/search?q=user@host` stay unchanged. `user:8080@host` is still a password. In the password, only a raw space, tab, CR or LF ends the run, so a quote or an encoded slash stays in the password. The separator is the last `@` before the first end-delimiter after a separator, and that delimiter may be a slash, `?` or `#` in any encoding. A later `@` outside the authority is left alone. Eight rounds are shared. A value that is still changing after that is redacted as a whole. Plain `%` and `%20` in prose are left as written. Ordinary words such as `task-12345678` are left as written. Vague quota text matches a Unicode word or an underscore-separated token, including `rate-limited`, `rate limiting` and `quota_exhausted`. `unexhausted`, `not exhausted`, `not_exhausted`, `not-rate-limited`, `isn't exhausted`, `quota is not exhausted`, `quota_is_not_exhausted`, `"not exhausted"` and `(not exhausted)` are not short faults. `quota is not available` and `no quota` still are. An OpenCode subscription meter is the normalised provider and sign-in method, so ChatGPT and Grok on one connection id do not share a breaker. A fence that fails while the driver is failing keeps the child and the driver error is what propagates, with the fence error chained under it. An `api_key_helper` route is its own meter and ignores `credential_env`. The wait time on a limit notice is the breaker's open window. A vendor reset that is earlier than that window stays on the signal and is not the advertised retry.

Lesson: [CSL-608](https://linear.app/csl-laboratories/issue/CSL-608) (Done research) found master's breaker
keyed by engine, shared by every tenant and never reset. This module does not claim to have measured
anything; no real limit capture has gone through it.

## Candidate and unwired

- No daemon calls `RouteService` yet; no real launcher (cgroup v2 leaf + pidfd) is in this module.
- Vendor field and option names marked "unverified" in the code were not read from vendor source
  (Claude `rate_limit_info` inner fields, Codex method names beyond `account/rateLimits/*`,
  `codexErrorInfo` values). `check_for_update_on_startup` was read in openai/codex source at
  `8703d32bd0ea5cf26768859a3dd476df906ba053`; no real-binary process-tree check has been run.
- Pi/Hermes/OpenCode stdio drivers are injected, not built here.
- No live provider call, real `claude`/`codex` process or network is used anywhere; tests use fakes only.

## Tests

From the repository root:

```
PYTHONPATH=core python3 -B -m unittest discover -s core/recursion_core/routes/tests -t core -v
PYTHONPATH=core python3.14t -X gil=0 -B -m unittest discover -s core/recursion_core/routes/tests -t core -v
```
