# core/recursion_core/subscription_routes (CSL-786, candidate)

Refuses Claude-subscription routes in Pi, Hermes and OpenCode engine profiles before any
spawn. Under Q12 the Claude subscription is used only inside Claude Code; other harnesses
use an Anthropic API key. Candidate code: not yet called by a running core, and not
reviewed or accepted.

- `check_route(engine, profile, child_env, route_home=..., parent_env=..., operator_home=...)`
  validates one `pi`, `hermes` or `opencode` profile and the exact child environment, and
  returns a `RouteDecision` or raises `SubscriptionRouteRefused(engine, cause, detail)`.
- `guard_then_spawn(launcher, argv, ...)` calls `check_route` and only then the launcher,
  always with the checked env and `shell=False`. It refuses `env`, `shell`, `executable`,
  `preexec_fn`, fd, user and group options before any check. `launcher` and `argv` are
  trusted, core-owned inputs: argv is not parsed, so it must not carry a provider, config
  or login flag the profile did not choose.

What refuses:

- any of `ANTHROPIC_OAUTH_TOKEN`, `ANTHROPIC_AUTH_TOKEN` (Pi), `ANTHROPIC_TOKEN`,
  `CLAUDE_CODE_OAUTH_TOKEN` (Hermes) in the child env, for every engine;
  `CLAUDE_CONFIG_DIR` in the child env;
- a Claude-subscription provider or auth kind (OpenCode's Claude Agent provider, Pi and
  Hermes Anthropic OAuth), and any provider, auth kind or credential variable not on the
  engine's explicit API-key list (fail closed);
- engine config, home and store selectors (`HERMES_HOME`, `PI_CODING_AGENT_DIR`,
  `OPENCODE_CONFIG`, `OPENCODE_CONFIG_DIR`, `OPENCODE_CONFIG_CONTENT`, `XDG_CONFIG_HOME`,
  `XDG_DATA_HOME`, `XDG_STATE_HOME`, `XDG_CACHE_HOME`), and, failing closed, any child env
  name other than `PATH`, `HOME`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TERM`, `TZ`, `TMPDIR` and the
  one selected API-key variable. Proxy and certificate names refuse here until the
  owners decide which ones these engines need;
- stored-credential adoption: the child's HOME must be the route home, private, not the
  operator's home, and must hold no Claude Code store entry (`.claude`, `.claude.json`,
  `.config/claude`) and no Pi, Hermes or OpenCode state or login entry (`.pi`, `.hermes`,
  `.local/share/opencode`, `.config/opencode`, `.codex/auth.json`). So the route home must
  be fresh at spawn. Checked with `lstat` only; no credential file is opened.

A subscription variable present only in the parent env is flagged in `RouteDecision.flags`.
API-key routes (`anthropic`, `openai`) launch.

## Subscription sign-ins (Nov 8 briefing)

Lucas's Nov 8 briefing: allow a subscription sign-in only where the provider names that
harness. A profile asks for one with `auth: "subscription"` and a `method`, and both are
checked against `SUBSCRIPTION_SIGN_INS`:

| Harness | Allowed (provider, method) |
|---|---|
| OpenCode (E3) | `chatgpt` + `sign_in_with_chatgpt`; `grok` + `grok_sign_in` |
| Pi (E8) | `chatgpt` + `sign_in_with_chatgpt` |
| Hermes (E9) | `grok` + `grok_sign_in` |

- Case, spaces, hyphens and underscores are spelling only ("Sign in with ChatGPT" matches).
  No other alias is accepted: the engines' native provider and method ids are not in
  repository evidence, and `grok_sign_in` is Recursion's label for a method the briefing
  allows without naming it.
- Claude (`claude*`, `anthropic*`) and Google (`google*`, `gemini*`, `antigravity*`) sign-ins
  refuse in all three. A provider not on the harness's row is `subscription_unconfirmed`; a
  wrong method is `subscription_method_not_registered`.
- `borrow_claude_code_login` must be absent or `false` (Hermes can otherwise use Claude Code's
  stored login).
- The child env carries no credential at all: no API key, no token, no `CODEX_*`. The sign-in
  lives in the engine's own login store in the route home (`.pi`, `.hermes`, or OpenCode's
  `.local/share/opencode` and `.config/opencode`), which the check allows for that engine only,
  by `lstat`. The own store may not be a link or sit under one (`own_store_is_link`), and no link
  inside it may resolve outside the route home (`own_store_links_outside_route_home`), so it cannot
  reach the operator's store. Every other engine's store, `.codex/auth.json` and every Claude Code store still
  refuse. Recursion never stores, copies or ships these tokens and cannot see what else a store
  holds without reading it, which it does not do.

## Own-loop routes: Copilot and Nous (Lucas, Nov 8)

PRODUCT.md `055cc7a5` section 3.3. `own_loop.py`, checked before any spawn or request, fail closed:

- **Copilot, "Copilot's own sign-in"**: `check_copilot_own_loop(route, parent_env, has_copilot_login=...)`
  with `{"engine": "copilot", "auth": "subscription", "method": "copilot_cli_sign_in"}`. The Copilot
  SDK runs on the user's stored `copilot` CLI login. Recursion passes no token: a route field that
  looks like a token, key or credential refuses, and `GITHUB_COPILOT_API_TOKEN`,
  `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN` and `COPILOT_API_URL` (plus the `gh`
  selectors `GH_HOST`, `GH_CONFIG_DIR`, `GH_ENTERPRISE_TOKEN`, `GITHUB_ENTERPRISE_TOKEN`) are
  removed from that run's child, as is every other provider credential (`*_API_KEY`, `*_TOKEN`, for
  example `ANTHROPIC_API_KEY`). An optional `model` is checked (printable, no spaces, at most 128
  characters) and carried on the decision. With no stored `copilot` login (or no probe, or a failing probe)
  the run refuses rather than fall back to `gh`. The probe is the caller's; nothing here opens a
  login store.
- **Nous, "Local proxy"**: `check_nous_own_loop(route, parent_env, ...)` with
  `{"engine": "nous", "auth": "subscription", "method": "hermes_local_proxy", "base_url": ...}`.
  The own loop calls only the user's own Hermes subscription proxy at a loopback IP literal
  (`127.0.0.0/8`, `::1`, or IPv4-mapped loopback); names, including `localhost`, are not resolved
  and refuse. Adapters are `["nous"]` only; `xai` refuses (`adapter_must_stay_off`). Recursion holds
  no Nous token: token-like route fields and URL credentials refuse, and provider credential
  variables are removed from the child. Proxy settings (`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, any
  case) are removed too, so loopback-bound traffic cannot be carried off the host. An optional
  `model` is checked and carried; an adapter-prefixed id must name `nous` (`xai/grok-3` refuses
  `adapter_must_stay_off`).
- `nous_proxy_doctor(base_url, current_user=..., interactive_users=[...])` reports the address,
  refuses one that is not loopback, and warns when other interactive users share the host (any
  local user can reach a loopback proxy, which "accepts any bearer").
- Still *[to verify]* in PRODUCT.md (O61): GitHub's Generative AI Services terms for Copilot, and
  whether the Nous Portal terms' consent clause covers an app calling through the proxy. No
  provider, DNS or network call is made here.

## Gemini CLI (E7)

`check_gemini_cli_sign_in(route)`: Gemini CLI consumer accounts ended June 18, 2026. Only a
paid API key (`{"method": "api_key", "paid": true}`, the user's statement; not checkable
offline) or an existing Code Assist Standard or Enterprise licence
(`{"method": "code_assist", "licence": "standard"|"enterprise", "existing": true}`) passes.
Consumer, free-tier and unknown methods refuse. No Gemini CLI route adapter exists yet, so
nothing calls it.

## Tests

From the repository root (Python 3.11+, standard library only):

```sh
python3 -B -m unittest discover -s core/recursion_core/subscription_routes/tests -t . -v
```

`tools/ci_gate.py` does not discover `engine/` tests; they run only when started by hand.

## Evidence

Candidate csl-research records (links only; the research stays there):
`submissions/20261107-csl786-subscription-routes-01` (this change's record), and the CSL-712
grounding records on csl-research branch `grounding-1077` (engine CLIs V51 and V56; vendor
terms H1 to H3).
