# routes: one route contract, Claude Code and Codex adapters, connection-keyed breaker (CSL-797)

Candidate code for build item 7. Spec: PRODUCT.md at csl-research `055cc7a5c793b7135e5d0565dc4198cf96230531`
(SHA-256 `e64783152f6d67d4ede774dc41d112fe46c2d7536d53cb2c04e71a3566044daf`, receipt pointer CSLR-D-011; first written
against `398cd812`, whose 3.4 S3/S4, 11.2 and Q12 text 055cc7a5 keeps): 3.4 S3 (breaker), S4 (hung
engine), 11.2 (breaker, plan limits), Q12 (Claude subscription only inside Claude Code).

## What it is

- `contract.py`: the route contract. Requests, results, events and limit signals are plain dicts that
  round-trip through JSON; every refusal or failure has a typed code (`CODES`). Surfaces and the daemon
  call `RouteService.handle(request) -> result` and nothing else, so the tests drive behaviour through
  the protocol and a later port can reuse them. The engine a route names is checked against
  `engine_names()` at the time of the call: today's five (`BASE_ENGINES`) plus the engines the
  composition registered in its engine table (seam S3). The table belongs to one composition, so it
  reaches the check through the route service: `RouteService(..., engines=<table>)`, or, by default, the
  engines the service has an adapter for (the daemon builds those from the same table). A name that is
  only in S3's catalogue stays `engine_unknown`; with nothing registered the answer is the five, as before.
- `claude_code.py`: `ClaudeCodeAdapter`, `claude -p --input-format stream-json --output-format stream-json`.
  Reads typed limit signals only: `rate_limit_event` with `status: rejected`,
  `api_error_params.rate_limit_info` on an error `result`, and a typed `error: "rate_limit"`.
  `system`/`api_retry` is a `retry` event and does not open the breaker. `seven_day_opus` and
  `seven_day_sonnet` fence that model family; other `rateLimitType` values fence the connection.
- `codex.py`: `CodexAdapter`, `codex app-server` (JSON-RPC over JSONL on stdio). Reads
  `account/rateLimits/read`, `account/rateLimits/updated` and typed JSON-RPC / turn errors.
  A named `limitId` is the breaker key. An `api_key` route ignores a ChatGPT-auth error from the
  rate-limit read and still runs the turn.
- `breaker.py`: `ConnectionBreaker`, keyed by the connection a route is billed through
  (`subscription:<login>` or `api_key:<key id>`), per model only when the typed signal names a per-model
  vendor bucket. Never keyed by engine id or tenant.
- `profile_route.py`: Pi, Hermes and OpenCode routes; every launch goes through
  `recursion_core.subscription_routes.guard_then_spawn` (B3, CSL-786), so a Claude-subscription provider is
  refused as `subscription_route_refused` before the launcher is called. Pi and Hermes also pass
  `route.wrapper` (the measured launcher and package pin); without it the launch is
  `wrapper_pin_missing`. The launcher receives the env `check_route` accepted.
- `launch.py`: the injected launcher / process interface, `require_free_threaded()` (refuses a GIL build;
  the daemon calls it before starting processes and after each plugin load) and `child_env()` (explicit
  env from an allowlist; never `os.environ`).
- `driver.py`: driver contract version 2 (lane SPINE's `driver.v2` schema, draft 4, in #368). A driver
  declares its engine, eleven flags (`multi_turn`, `mid_turn_input`, `interrupt`, `resume`, `fork`,
  `permission_bridge`, `typed_limits`, `usage`, `observes_subagents`, `cancels_subagents`,
  `usage_complete`) and `input_ack`. `Driver` is the gate every engine's driver
  subclasses: a call whose flag is false raises `DriverRefused` (`unsupported`, the call, the flag) before
  the subclass is asked, so no process starts and no byte reaches the engine; a malformed call or one the
  worker's state does not allow (closed input, a reused turn or delivery id, an interrupt of a turn not in
  progress) is refused the same way. `parse` returns only the event types a driver may draft, with exactly
  their keys, and an `input.accepted` only when it matches a delivery this worker wrote, with the turn and
  the `how` of that write; anything else is dropped and counted. `one_turn(engine)` declares today's path,
  `RouteAdapter.run`, with every flag false. No engine has a driver yet (later slices: Claude Code's).
  A handle's writes reach the engine in the order the gate reserved them, close and interrupt included: a
  per-handle write lock is held from the state check through the subclass's write, while the state lock
  stays free for the reader in `parse`. `start` checks the nested route and workspace (common.v1) before
  the subclass starts anything, and `parse` checks every payload value and relation of event.v1 (enums,
  identifiers, digests, timestamps as real instants, `text` xor `output_ref`, `model` exactly when a limit's
  scope is `model`), against #368 as pinned at `c77aa2e0`, in plain code. The calls, for the engine lanes:
  `start(session, workspace, env) -> Handle` (`session` is `{session_id, run_id, route}`),
  `resume(engine_session_id, session, workspace, env) -> Handle`, `send(handle, input)`,
  `interrupt(handle, turn_id)`, `fork(engine_session_id, at) -> engine_session_id`,
  `parse(handle, line) -> [draft]`; a subclass implements `_start`, `_send`, `_interrupt`, `_fork` and
  `_parse`, calls `turn_ended(handle, turn_id)` at the engine's end-of-turn sign and
  `handle.set_engine_session_id(id)` when the engine reports its conversation id. PROVISIONAL, by the
  Liaison's ruling of Nov 14 (5:01 PM): the start and resume forms follow the frozen brief rather than
  driver.v2's one start record (the subclass still receives that record), and a user_message sent into a
  turn in progress, on a driver without `mid_turn_input`, answers the brief's `busy` (`DriverBusy`, a
  `DriverRefused`) rather than driver.v2's `unsupported`. Which stands is Lucas's; consumers must not rely
  on either yet.
- `host.py`: `platform_supported()`, the one place in this package that reads `os` (`os.name` and whether
  `os.geteuid` exists, the same test as `child_env` and `subscription_routes`). The adapter modules still
  never import `os`.

## Rules it enforces

- Windows refuses at the shared adapter boundary (Lucas, Nov 12, decision sheet D9: "Windows refuses
  uniformly at the shared adapter boundary, before any path or connector is looked at"; R3-7).
  `RouteAdapter.run`, which the Claude Code and Codex adapters share, answers `launch_refused` with detail
  `platform_unsupported` before it builds an argv or an environment, asks for a credential or calls the
  launcher. The profile routes keep their own refusal, `subscription_route_refused` with the same detail,
  from `subscription_routes`. Route tests that run an adapter through a fake launcher carry
  `fakes.runs_adapter`, which for the test's duration lets the shared boundary's host check pass, so their
  protocol and environment assertions run on Windows too, against fakes; `tests/test_r3_finding_7.py`
  asserts the refusal itself with the real host check.
- Only a typed limit signal opens a connection; it fences every tenant on that connection and no other
  connection. Text inside the model's payload (assistant/user content, Codex agent messages) is never
  inspected. Vague quota-sounding error text is a `short_fault`: one model, one tenant, 60 s (grill Q23).
  The refusal while that fence holds is `short_fault` too, never `breaker_open`, so it gets no
  `limit_notice` and never triggers a fallback. Open and half-open keys are checked before it, so a
  short fault never hides a real limit on the same connection from that tenant.
- Recursion's own deadline is `deadline_exceeded`: the run is fenced (`EngineProcess.fence`) for that
  request only and the breaker is not told. Other engine failures are request-local too. The exception is a
  deadline whose fence fails (next point): then no typed answer is given, the `Deadline` itself leaves
  `RouteAdapter.run`, and `RouteService._run` records `engine_error` with the breaker and re-raises it.
- A child started by `RouteAdapter.run` is fenced on every way out of that method, once the launcher has
  returned it and the method holds it; an asynchronous exception (a signal handler's) delivered between the
  launcher's return and that store, or inside the method's last handler before its fence, is not covered
  (`base.py` names the window). A successful return is not fenced. The three typed exits (deadline, protocol error, engine
  io) fence with their own reason and answer. Any other exception, `BaseException` included, fences the
  child with the reason `unexpected`, and the same exception object leaves. If a typed exit's fence raises,
  the original typed exception leaves instead of the answer, after one more fence as `unexpected`; the
  failure is noted on it (`add_note`). A fence failure never replaces the exception that leaves; an
  interruption raised by a fence itself is recorded as a cleanup failure and does not stop the retry. When no
  fence succeeded, that exception also carries `cleanup_failures` (`base.CLEANUP_FAILURES`, the failures
  oldest first): the child may still be running (or already gone: the attribute says only that no fence
  succeeded), and a caller must not treat the exit as clean. The attribute is set even on an exception whose
  own `__setattr__` refuses it (a frozen dataclass, say), through `BaseException`'s own setter. The notes are
  best effort: an exception whose `add_note` refuses gets its first note the same way only if it has none. A driver
  whose adapter runs this `run` does not need to fence for itself; an adapter that overrides `run` owns its
  own cleanup (`ProfileRouteAdapter` does: lane ENGINE-E9's #425 adds its fence). The boundary fences only a
  process the launcher returned: a launcher that fails after starting a child must end that child itself
  before it raises (`launch.py`). Tests: `tests/test_base_fence.py`.
- Recovery: an open key always has a finite end (`reset_at` on the breaker clock, capped; otherwise a
  300 s cooldown doubling to 3600 s). Vendor `resetsAt` is Unix epoch and is translated onto that clock
  before it is stored, so a monotonic deadline clock does not turn one limit into an 8-day fence.
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
  route, or cancel). Not reported is the same question, and the same answer, `billing_ack: "api_key_helper"`,
  runs it (R3-13 part 1: the question offered that answer and the adapter then refused it).
- `api_key`: the user's own key from custody (`ANTHROPIC_API_KEY`) or their own `apiKeyHelper`
  (`key_source: "api_key_helper"`, no key added); `--bare` is allowed (`route.bare`). The user's
  `CLAUDE_CODE_OAUTH_TOKEN` still passes through; the key outranks it by the engine's own precedence.
- `passthrough` (a cloud provider or gateway the user picks, `connection.method`: `bedrock`, `vertex`,
  `foundry` or `gateway`): refused before launch, by this adapter, for every method: `launch_refused`,
  `no credential custody: a passthrough route is not launched`, the daemon's own words (Lucas, Nov 12,
  decision sheet A2: "The daemon refuses routes with key_source: api_key_helper and cloud passthrough
  routes until credential custody is built"; A8: the gateway method stays refused with the cloud ones;
  R3-8). `env()` refuses before it reads a variable, so no caller of the adapter launches one. The method
  rules below are kept in `passthrough_env()`, which no launch path calls; before one does, R3-8's open
  point has to be settled (a gateway still receives the user's `CLAUDE_CODE_OAUTH_TOKEN` and an
  `ANTHROPIC_BASE_URL` that may carry credentials). The rules: that method's own variables (names from
  Claude Code's public docs) pass through
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
- Under the daemon, none of that runs for an `api_key` route with `key_source: "api_key_helper"`, for a
  `passthrough` route, or for a route whose `limits` would switch to one (`on_limit: "fallback"`): the
  daemon refuses the start (`start_failed` / `launch_refused`, `no credential custody`) before this
  adapter is called (`daemon/control_adapter.py` `route_unsupported`; `daemon/README.md`, "Not done").
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
Every attempt shares the request's one deadline. Nothing picks a route by itself; `fallback` with an empty list is refused.
A fallback route may carry the keys a primary route's adapter reads, `wrapper` (the Pi and Hermes launcher
pin) among them (R3-13 part 2); `subscription_routes` still refuses a wrapper that is not a measured pin
when the route is launched. Any other key is `request_invalid`.

Lesson: [CSL-608](https://linear.app/csl-laboratories/issue/CSL-608) (Done research) found master's breaker
keyed by engine, shared by every tenant and never reset. This module does not claim to have measured
anything; no real limit capture has gone through it.

## Candidate and unwired

- The daemon calls `RouteService` for Claude Code and Codex with a real launcher
  (`recursion_core/daemon/launcher.py`, a transient user unit and a pidfd; see `daemon/README.md`,
  "Composition"). No real launcher is in this module, and Pi, Hermes and OpenCode are not wired.
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
