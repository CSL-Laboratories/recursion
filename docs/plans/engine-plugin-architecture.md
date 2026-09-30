---
title: Declarative engine plugin architecture
type: implementation-plan
status: authoring
last-verified: 2026-08-26
related:
  - CONTROL_PLANE.md
  - docs/plans/hermes-project-pilot-production-plan.md
  - docs/plans/hermes-phase8-routing-recovery-handoff.md
  - scripts/work.sh
  - scripts/worker-sandbox.sh
  - scripts/resolve-model-alias.py
  - schemas/control-plane/v1/schema.json
---
# Declarative engine plugin architecture

Authorized by Lucas 2026-08-26 as a control-plane authoring session (see D-015 draft).
Motivation: subscriptions are being cancelled and moved; Recursion must be able to swap
worker engines (Cursor CLI, Gemini CLI, OpenCode CLI, a local Qwen arm on the Sentry PC)
and models without code changes, while every existing safety property is preserved.

## Objective

Turn "engine" from a hardcoded case statement into **data**: one JSON manifest per engine,
validated against a schema, resolved by a fail-closed resolver, executed by a generic
sandbox path. The machine gate, scope check, and independent verifier never depended on
the model — this makes the worker plane match that fact.

Inspiration (per Lucas): the DeepSeek harness / plugin methodology — capability as
declarative manifest + thin adapter, not per-provider code.

## Principles (invariants — violating any of these fails the gate)

1. **Additive and inert by default.** Builtin engines (`claude`, `hermes`, `gate`,
   `none`) keep their exact current code paths, byte-for-byte behavior. Plugin engines
   activate only when BOTH `RECURSION_ENGINE_PLUGINS=1` AND
   `RECURSION_ENGINE_<NAME>_ENABLED=1` are set in the human-owned `.env` on the box.
   The loop never flips these (CLAUDE.md rule 3). Absent flags → identical rejection to
   today (exit 92).
2. **Fail closed everywhere.** Unknown engine, invalid manifest, disabled plugin, missing
   executable, absent alias mapping, failed availability probe, metered engine without
   `RECURSION_ALLOW_METERED=1` — each is a distinct, tested refusal.
3. **No secrets in manifests.** Manifests carry env var *names* only. Credentials come
   from the runner home mount (already the mechanism for `claude`/`hermes`) or from
   controller env at launch. Values are never logged.
4. **Argv, never shell.** Prompt and model are substituted as exact single argv elements.
   No string interpolation of model output into a shell line, ever.
5. **The gate does not care who wrote the code.** Acceptance, scope verification, and
   implementer≠verifier separation are unchanged; cross-*engine* verification becomes
   possible (implement on Cursor, verify on Gemini) and is encouraged.
6. **Unavailability is not failure.** A plugin engine whose availability probe fails
   (e.g., the Sentry PC serving Qwen is off) causes the task to remain queued with a
   backoff note — never a blocked task, never an escalation. That is the whole
   "toggle-able local arm" design: the toggle is whether the server answers.

## Component 1 — engine manifests

`scripts/engines/<name>.json`, one per plugin engine. Schema at
`schemas/control-plane/v1/engine-manifest.schema.json` (new).

```json
{
  "schema_version": 1,
  "name": "gemini",
  "description": "Google Gemini CLI, headless",
  "executable": "gemini",
  "argv": ["{{EXE}}", "-p", "{{PROMPT}}", "--yolo"],
  "model": { "required": false, "argv_suffix": ["--model", "{{MODEL}}"] },
  "roles": ["implementer", "verifier"],
  "billing": { "basis": "metered", "provider": "google-gemini" },
  "network": { "policy": "provider-only",
               "endpoints": ["generativelanguage.googleapis.com"] },
  "availability_probe": null,
  "concurrency_cap": null,
  "env_passthrough": []
}
```

Field rules:

- `name`: `^[a-z][a-z0-9-]{1,31}$`, must equal the filename stem, must not collide with
  builtin engine names (`claude`, `hermes`, `gate`, `none`, `deepseek`, `openrouter`).
- `executable`: basename only (resolved via `command -v` inside the sandbox controller,
  then bind-mounted read-only exactly like the existing single-executable path).
- `argv`: array of strings. Placeholders `{{EXE}}`, `{{PROMPT}}`, `{{MODEL}}` are
  replaced only when a whole element equals the placeholder. `{{EXE}}` must be element 0.
  `{{PROMPT}}` must appear exactly once. No other templating.
- `model.required`: if true, launch without a resolved model is refused.
  `model.argv_suffix` is appended only when a model is present.
- `billing.basis`: `flat-subscription` | `metered` | `local`. `metered` engines are
  admitted only under `RECURSION_ALLOW_METERED=1` (same rule as `engine=hermes` today).
  The usage-evidence policy record is generated from this block (see Component 4).
- `network.endpoints`: documentation + deployment input for the provider-only
  allowlist. Enforcement stays in the root-owned provider netns/proxy on the box; a
  plugin engine enabled before its endpoints are provisioned simply has no egress and
  fails its run — fail closed, no new capability implied by the manifest.
- `network.profile`: required reviewed provider-egress profile. Its provider and endpoint
  set must match `billing.provider` and `network.endpoints`; a provider-only manifest
  without a profile is invalid.
- `availability_probe`: `null`, or
  `{ "type": "http", "url_env": "RECURSION_ENGINE_<NAME>_HEALTH_URL", "timeout_s": 5 }`.
  The URL itself lives in human-owned env, never in the manifest.
- `concurrency_cap`: `null` or a positive integer — maximum simultaneous runs for this
  engine across the box (the Sentry arm ships with 1).
- `env_passthrough`: array of env var *names* copied from the controller environment into
  the unit when set. Names are accepted only when they appear in the exact reviewed
  allowlist attached to `network.profile`; the worker re-checks this binding before it
  consumes any name. Values never appear in manifests or command lines.

Shipped manifests (all inert until enabled on the box):

| File | Engine | Notes |
|---|---|---|
| `gemini.json` | Gemini CLI | headless `-p`; metered (API key) or sub via runner-home OAuth |
| `cursor.json` | Cursor CLI (`cursor-agent`) | headless print mode; `--model` passthrough gives the multi-model flexibility; billing `metered` until Cursor emits usage evidence |
| `opencode.json` | OpenCode CLI | fronts OpenRouter/DeepSeek/any OpenAI-compatible provider; supersedes the stubbed `deepseek`/`openrouter` engines |
| `opencode-local.json` | OpenCode → Sentry PC | Qwen 30B-class via local OpenAI-compatible server over the tunnel; `availability_probe` http; `concurrency_cap` 1; billing `local` |

**Exact CLI flags in these manifests are provisional until verified on the box** (the
handoff rule: measure, don't assume). Each manifest is inert data until then, so shipping
provisional argv is safe.

## Component 2 — engine resolver

`scripts/resolve-engine.py` (new). Pure, offline, no side effects except the emitted plan.

```
resolve-engine.py <engine> --role implementer|verifier [--model-present] --emit-plan <out.json>
```

- Loads `scripts/engines/<engine>.json`, validates against the schema and the field rules
  above (self-contained validation — no external jsonschema dependency, matching
  `resolve-model-alias.py` style).
- Checks enablement: `RECURSION_ENGINE_PLUGINS` global gate, then per-engine flag.
- Checks the role is in `roles`.
- Emits a **plan**: normalized JSON with final argv (placeholders still present as
  placeholders — substitution happens in the sandbox with exact-element matching),
  billing, probe spec, concurrency cap, env_passthrough.
- Exit codes: `0` ok; `2` invalid manifest / role not permitted / usage error;
  `3` plugins disabled, engine disabled, or manifest absent (human-owned mapping absent —
  mirrors the alias resolver's 3); nonzero never emits a plan.
- `RECURSION_ENGINE_DIR` (manifest directory override) and `RECURSION_ALIAS_REGISTRY`
  (alias registry override) are **test-only hooks** for hermetic controls. Never set
  them on the box: the shipped locations under `scripts/` are the reviewed truth.

## Component 3 — model alias generalization

`scripts/resolve-model-alias.py` v2 plus a data registry
`scripts/engines/model-aliases.json`:

- The three legacy aliases (`sonnet-routine`, `opus-complex`, `opus-review`) keep their
  exact env names, role sets, and behavior. Regression-tested byte-identical output.
- New aliases are **data**: the registry maps alias → `{env, roles}`. Shipped starters:
  `cursor-primary` (both roles), `gemini-review` (verifier), `opencode-flex` (both),
  `sentry-local` (implementer). Humans extend the registry by editing the JSON (a
  reviewed control-plane change), and set the env values in `.env`.
- Env values may now be `engine:model` (e.g.
  `RECURSION_MODEL_ALIAS_CURSOR_PRIMARY=cursor:gpt-5.2-codex`). The resolver validates
  the engine part against `^[a-z][a-z0-9-]{1,31}$` and prints the value through; a bare
  value (no colon) keeps meaning "model for the task's declared engine" — full backward
  compatibility.
- `work.sh` splits an `engine:model` resolution: the engine part must equal the task's
  `engine:` field or the run is refused pre-worker (`model-alias-engine-mismatch`), so an
  alias can never silently reroute a task to a different engine.
- Unsafe-character screening, absent-mapping exit 3, and unknown-alias exit 2 unchanged.
- `schemas/control-plane/v1/schema.json`: `model_alias` / `verifier_model_alias` become
  `anyOf` [existing enum, pattern `^[a-z][a-z0-9-]{2,31}$`]. The registry+resolver are
  the authoritative fail-closed check for non-enum aliases; implementer ≠ verifier
  remains enforced in `work.sh`.

## Component 4 — work.sh integration

At the engine dispatch (`case "$ENGINE"` in `launch_worker`, and the pre-worker contract
checks):

- `none`, `claude`, `hermes`, `gate`: unchanged.
- `deepseek|openrouter`: keep the exit-91 stub, with a comment pointing at the
  `opencode` manifest as the supported route (raw chat-completion never grew file tools;
  OpenCode subsumes it).
- `*`: call `resolve-engine.py`. Exit 3 or 2 → the existing invalid-engine rejection
  path (externally identical to today's 92). Exit 0 → plugin path:
  1. **Billing admission**: `basis=metered` requires `RECURSION_ALLOW_METERED=1`
     (reuse the hermes 93 path). `flat-subscription` and `local` proceed.
  2. **Usage evidence**: write the policy record from the manifest
     (`provider`, `billing_basis`, `evidence_source:"engine-manifest-v1"`) exactly the
     way the claude flat-subscription record is written today. Absent record stays a
     hard failure.
  3. **Availability probe**: if the manifest declares one, resolve the URL env; probe
     with a bounded timeout, from inside the provider-only namespace when it exists (a
     controller-network probe can be green while the worker has no route). Failure →
     **defer disposition**: log `engine unavailable`, remove the emitted plan files, and
     `exit 75` — the same defer callers already handle for a held project lock or a
     window-authority deferral. The task stays queued; no board entry, no escalation,
     no `blocked`. (As shipped this reuses the established 75 defer rather than a new
     exit path; a deferring task is visible in `loop/logs` only.)
  4. **Concurrency cap**: acquire one witnessed per-slot lock
     (`state/.locks/engine-<name>-slot-<i>.lock`, held for the run, released by the
     EXIT trap) — a unit count would be stale by launch time and race across projects.
     No free slot → same defer disposition.
  5. **`model.required`**: a plan that requires a model refuses admission (and,
     independently, sandbox launch) when no model is resolved — otherwise the engine
     would run on its own configured default, off-alias and off-evidence.
  5. Launch via `worker-sandbox.sh --engine <name> --engine-plan <root-owned plan file>`.
- Pre-worker contract additions: `model-alias-engine-mismatch` (Component 3), and the
  existing `model-separation-invalid` check extended so that *engine-level* separation is
  recorded in run evidence when implementer and verifier resolve to different engines.

## Component 5 — worker-sandbox.sh integration

- Engine validation (`case` at the top): builtin names unchanged; any other name is
  accepted **only** with `--engine-plan <path>` where the plan file is root-owned,
  not group/other-writable, and parses as a valid plan. Anything else → 92.
- Executable resolution: from the plan's `executable` via `command -v` (missing → 90),
  then the existing single-executable bind-mount path (`$SANDBOX_BIN`) verbatim — plugin
  engines get the identical mount treatment as a non-`/usr` claude.
- Exec construction: generic branch builds the systemd-run command from plan argv with
  exact-element substitution of `{{EXE}}` → mounted executable path, `{{PROMPT}}` →
  `--prompt` value, `{{MODEL}}` → `--model` value (append `model.argv_suffix` only when
  a model was passed). `env_passthrough` values ride a root-owned `0600`
  `EnvironmentFile` (removed on cleanup), never the systemd-run command line — argv is
  world-readable in `/proc` for the run's whole duration and these are API keys.
- Provider egress is profile-bound and generic: the admitted plan supplies the provider,
  endpoint set, namespace, and loopback proxy port. Every provider-only engine receives
  only that profile's proxy environment; no Claude/Anthropic default or engine-specific
  proxy injection is permitted.
- Logical test mode (`RECURSION_TEST_WORKER=logical`, /tmp-only) gets the same generic
  branch so hermetic controls can exercise a stub executable on PATH.

## Component 7 — cross-engine verification (added 2026-08-26 at Lucas's request)

A task may carry `verifier_engine:` naming a different engine for the independent
verifier than `engine:` names for the implementer (implement on Cursor, verify on
Gemini). Semantics:

- Absent field → the implementer's engine; every existing task behaves identically,
  and the contract digest of a task without the field is unchanged (the key joins the
  digest set, but only present fields are hashed).
- Each role's engine is validated independently: strict mode rejects
  `hermes`/`gate`/`none`/`deepseek`/`openrouter` as a verifier engine; plugin engines
  resolve their own role-specific plan at admission; billing, `model.required`,
  probe, and capacity-slot checks run per engine (one slot when both roles share an
  engine, one each when they differ).
- An engine-qualified alias value must match the engine of **its role** —
  `verifier_model_alias` resolving to `gemini:...` is valid only when
  `verifier_engine: gemini`.
- The shared rules live in `scripts/engine_contract.py`, consumed by
  `project-pilot.py` (pilot plan admission now accepts claude or a well-formed plugin
  engine — hermes stays refused for pilots — and registry aliases with role
  enforcement) and `validate-control-state.py`. `resolve-model-alias.py` keeps its
  self-contained copy by design: it is the execution-time authority.
- Pilot plan tasks may name `verifier_engine`; the projection renders it only when
  present, and any pilot task with `network_policy: none` is now refused regardless
  of engine (every admissible engine is a networked worker).

## Component 6 — connectors and further pluggability (scaffolded 2026-08-26; inert)

The manifest pattern now extends to notification transports:
`schemas/control-plane/v1/connector-manifest.schema.json` documents the contract,
`scripts/resolve-connector.py` is the fail-closed authority (0 valid / 2 invalid / 3
disabled-or-absent, gated on `RECURSION_CONNECTORS=1` plus
`RECURSION_CONNECTOR_<NAME>_ENABLED=1`), and `scripts/connector-transport.py` is an
adapter a human may later point notify.sh's existing, currently-unset
`RECURSION_NOTIFY_TRANSPORT` hook at. **`notify.sh`, `notification-outbox.py`, and
`transition_receipt.py` were not modified**, so D-014's promotion evidence is intact;
`scripts/connectors/` ships zero loadable manifests (a README plus one `.example` the
resolver cannot load), and the adapter ships mode 644 so notify.sh cannot exec it until
a human chmods it. The adapter substitutes `{{DESTINATION}}`/`{{MESSAGE}}` as exact argv
elements, filters the child environment to `env_passthrough` plus PATH/HOME/LANG,
enforces `timeout_seconds`, and passes the child's stdout through byte-exact — it never
parses, edits, or fabricates an acknowledgement; notify.sh stays the sole ack validator.
`env_passthrough` refuses any name containing TOKEN/SECRET/KEY/PASSWORD with no
allowlist, so the Telegram token still never leaves notify.sh.

Gated on D-014 closing, deliberately not done: widening notify.sh's ack validator past
`transport=="telegram"` (until then `ack_contract` is a one-member enum — adding members
first would make every delivery fail); mapping the adapter's retryable exit 10 through
notify.sh's transport branch (it currently flattens nonzero to 12); non-Telegram
destination kinds in the outbox; any enablement.

Supervisor-plane routing (Hermes `config.yaml`, `loop/config/routing.yaml`) is already
human-owned configuration and stays out of scope.

## Controls (red before green)

1. `scripts/tests/engine-manifest.test.py` — every shipped manifest validates; mutations
   (missing field, bad name, argv without `{{PROMPT}}`, `{{EXE}}` not first, secret-like
   value in `env_passthrough`, builtin-name collision) are each rejected.
2. `scripts/tests/resolve-engine.test.py` — unknown engine 3; plugins globally disabled
   3; per-engine disabled 3; role not permitted 2; valid+enabled emits a correct plan;
   metered basis surfaces in the plan; probe spec and concurrency cap pass through.
3. `scripts/tests/resolve-model-alias.test.py` — the three legacy aliases resolve
   byte-identically to v1; registry aliases resolve; `engine:model` values pass through
   intact; unknown alias 2; absent env 3; unsafe characters 2; role mismatch 2.
4. `scripts/tests/worker-sandbox-plugin.test.py` — logical-mode stub engine receives
   prompt/model as single argv elements (quoting/injection control); plan file with wrong
   ownership/permissions refused; builtin engines still refuse `--engine-plan`.
   (Ownership/systemd assertions are Linux-only; the test skips them off-box and they
   re-run in the box suite.)
5. `work.sh` regressions, box-run: unknown engine with plugins unset rejects exactly as
   today; metered plugin without the flag hits the 93 path; probe-fail leaves the task
   queued and unescalated.

Red evidence: each test lands one commit before its implementation and must fail there;
the implementing commit turns it green. Same discipline as every phase since Phase 0.

## Deployment steps (box, human-gated — none performed this session)

1. Lucas reviews and merges the review branch; box pulls under the normal PAUSE-held
   procedure.
2. Verify each CLI's presence, exact headless flags, and auth on the box **as the runner
   user**; correct manifests if flags drifted; commit corrections. Install each CLI under
   `/usr/local/bin` (or elsewhere in `/usr`): the sandbox's single-executable copy cannot
   carry a `~/.local/bin`-style wrapper's sibling runtime tree, and a home-dir install
   would silently produce a broken exec.
3. Provision provider-only egress for each engine to be enabled
   (`network.endpoints` → the root-owned netns/proxy allowlist).
4. Set `.env`: `RECURSION_ENGINE_PLUGINS=1`, per-engine `_ENABLED=1`, alias values
   (`RECURSION_MODEL_ALIAS_*`), and for the Sentry arm
   `RECURSION_ENGINE_OPENCODE_LOCAL_HEALTH_URL` (tunnel address of the local server).
5. Sentry PC: serve the local model behind the tunnel (llama.cpp / LM Studio,
   OpenAI-compatible, quantized KV as desired). Turning the server off is the toggle;
   Recursion requeues rather than blocks.
6. Sequencing vs the promotion program: land this before running promotion gates 2–5 so
   the pilots and soak exercise the engine roster that will actually run in September.
   Gate-1 window work in flight is unaffected (all plugins inert until step 4).

## Addendum 2026-08-26 (same session, later) — telemetry, governance, hygiene

Rolled out on the same review branch after the cross-engine delta, on Lucas's instruction:

- `scripts/usage-reconcile.py` — turns the usage evidence work.sh already writes into an
  auditable cost ledger (`state/usage/ledger.jsonl`, idempotent by run_id) plus a one-page
  weekly rollup. The load-bearing output is the split, not the total: receipt /
  policy-record / unreconciled per run, with metered-but-receipt-less engines (Cursor)
  printed as spend-UNKNOWN. Opt-in `openrouter-snapshot` adds account-level credit
  snapshots as a coarse cross-check, never attributed per run.
- `scripts/engine-scorecard.py` — per-(engine, alias) task statuses, run outcomes,
  retries, wall-clock median/total, and attributable escalations from
  `state/tasks` + `state/events.jsonl` + `state/runs`; absent sources report as absent,
  malformed inputs are tallied, output is one page (`--json` for the full dump).
- `scripts/verify-branch-protection.py` + the plan.sh automerge-seam closure — esc-0001
  close-out tooling (exit 0 verified-clean / 1 violation / 3 unverifiable; the tokenless
  truth today is 8/8 remote rows unverifiable).
- worker-sandbox.sh fix, in two parts: the hermes usage-file BindPaths property was
  appended after the systemd-run command argv (it landed in hermes's argv instead of
  mounting), and the mount target itself sat under the arm's masked /run — systemd drops
  binds shadowed by InaccessiblePaths — so the file is now bound into the unit's
  PrivateTmp at /tmp/recursion-worker-usage.json. worker-evidence.py treats an empty
  usage file as "no usage data" instead of crashing finish. One metered hermes run on
  the box should confirm the file actually lands.
- Component 6 connector scaffolding (see the amended section above).
- `docs/audits/unlanded-branches-2026-08-26.md` — all 13 unlanded 2026-08-12/13 branches
  are already on master; deletion hygiene only.

Telemetry outputs land under `state/usage/` and `state/reports/` (both runtime-only,
gitignored). Proposed cron lines and the remaining human steps are in
`state/PROPOSALS.md` (2026-08-26 entry); the esc-0001 remainder is recorded inline in
`state/INBOX.md`.

## Non-goals

- No autonomy widening; no changes to PAUSE, publish, merge, or notification behavior.
- No `.env` writes, no cron changes, no model-routing changes (rule 3).
- No default-engine change: `engine: claude` tasks behave identically.
- No provider-proxy/netns code changes this session (deployment step 3 is config).
- No live enablement: every plugin engine — and every connector — ships disabled.
- No notify-transport execution-path change: Component 6 scaffolding is inert and
  notify.sh/the outbox are untouched (D-014 evidence preserved).
