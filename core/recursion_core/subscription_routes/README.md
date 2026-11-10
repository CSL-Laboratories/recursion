# core/recursion_core/subscription_routes (CSL-786, candidate)

Refuses Claude-subscription routes in Pi, Hermes and OpenCode engine profiles before any
spawn. Under Q12 as widened 2026-11-08 (PRODUCT.md `055cc7a5`, CSLR-D-011, sections 2 and 3.3) a
subscription runs through another harness's own sign-in only where that provider allows that
harness; Claude-subscription sign-ins stay refused in these three, and an Anthropic API key
still works. Candidate code: not yet called by a running core, and not
reviewed or accepted.

- `check_route(engine, profile, child_env, route_home=..., parent_env=..., operator_home=..., wrapper=...)`
  validates one `pi`, `hermes` or `opencode` profile and the exact child environment, and
  returns a `RouteDecision` or raises `SubscriptionRouteRefused(engine, cause, detail)`.
  Pi and Hermes require `wrapper` (see Wrapper pins). Cursor and Antigravity CLI use
  `check_cursor_route` and `check_antigravity_route`.
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

## Subscription sign-ins (2026-11-08 briefing)

Lucas's 2026-11-08 briefing: allow a subscription sign-in only where the provider names that
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
- A subscription route must name its `model` (`"<provider>/<model>"`, under the signed-in
  provider), so the engine's own default cannot pick a Claude model (`model_missing`).
- Every Hermes profile (sign-in or API key) must carry Hermes's own setting
  `"auth.adopt_external_logins": false` (PRODUCT.md `055cc7a5` section 2, E9); missing or any other
  value refuses `hermes_adopt_external_logins_not_off`. The decision records it in
  `RouteDecision.engine_config` for the driver to pass to Hermes. It is required on every
  platform: on macOS Claude Code's login sits in the login Keychain, which moving HOME does not
  hide. Pi and OpenCode profiles refuse the key as unknown.
- The child env carries no credential at all: no API key, no token, no `CODEX_*`. The sign-in
  lives in the engine's own login store in the route home (`.pi`, `.hermes`, or OpenCode's
  `.local/share/opencode` and `.config/opencode`), which the check allows for that engine only,
  by `lstat`. The own store may not be a link or sit under one (`own_store_is_link`), and no link
  inside it may resolve outside the route home (`own_store_links_outside_route_home`), so it cannot
  reach the operator's store. Every other engine's store, `.codex/auth.json` and every Claude Code store still
  refuse. Recursion never stores, copies or ships these tokens and cannot see what else a store
  holds without reading it, which it does not do. So a present own store is flagged in
  `RouteDecision.flags` as `own_login_store_not_inspected:<entry>`: it could hold a login
  `hermes auth` adopted or an OpenCode Claude provider plugin. Reading those stores (provider
  entry presence only) or refusing them is open for Lucas.

## Own-loop routes: Copilot and Nous (Lucas, 2026-11-08)

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
  local user can reach a loopback proxy, which "accepts any bearer"). With no
  `interactive_users`, it and `check_nous_own_loop` warn "host users unknown" instead of
  reporting a clean host.
- Still *[to verify]* in PRODUCT.md (O61): GitHub's Generative AI Services terms for Copilot, and
  whether the Nous Portal terms' consent clause covers an app calling through the proxy. No
  provider, DNS or network call is made here.

## Gemini CLI (E7) and Antigravity CLI (2026-11-09)

Google, [Gemini Code Assist consumer accounts](https://developers.google.com/gemini-code-assist/docs/deprecations/code-assist-individuals):
on 2026-06-18 Gemini CLI stopped serving consumer accounts, and Login with Google no longer
works. Google, [transitioning Gemini CLI to Antigravity CLI](https://developers.googleblog.com/an-important-update-transitioning-gemini-cli-to-antigravity-cli/):
the subscription path is Antigravity CLI; Gemini CLI remains available with a paid API key.
The same post says Code Assist Standard and Enterprise licences are on a separate schedule.
Lucas's 2026-11-09 call keeps Gemini CLI here only as that paid API-key route.
`check_gemini_cli_sign_in({"method": "api_key", "paid": true})` passes (`paid` is the user's
statement; a tier cannot be checked offline). A Code Assist licence and every other
subscription sign-in refuse (`subscription_sign_in_dropped` or `consumer_account_ended`).
No Gemini CLI route adapter exists yet, so nothing calls it.

`check_antigravity_route` is the beta wrapper for `agy`. Its pin is the version plus the
sha256 of the `agy` binary and of its launcher. Package files are not part of that pin.
A mismatch refuses `wrapper_pin_mismatch` until the pin is replaced. The binary is not run.
T3 Code is background only: this package has no T3 Code route.

The floor table's Measured help column (which builds have a measured `--help`) already
landed on recursion #323. It is not repeated here.

## Wrapper pins (Pi, Hermes, Cursor)

Lucas, 2026-11-09: Pi, Hermes and Cursor run as beta when the launcher script and the
installed package files match a pin. `measure_package_pin(launcher, install_root)` hashes
the launcher and every file under the install root (a symlink is hashed as its link text,
and directory symlinks are not followed). Pass that pin as `wrapper=` to `check_route`
(Pi, Hermes) or `check_cursor_route`. A changed, extra or missing file, or a different
launcher, refuses `wrapper_pin_mismatch`. OpenCode is not in this pin. Cursor's child env
carries no credential. A missing pin on Pi or Hermes refuses `wrapper_pin_missing`.

`guard_then_spawn` launches the env `check_route` accepted, not a second read of the
caller's mapping. A subscription `model` must appear in argv (or as `--model` / `-m`);
otherwise the launch refuses `model_not_bound`. An empty model segment (`anthropic/`)
refuses `model_name_empty`. Hermes's `auth.adopt_external_logins: false` is written to
`.hermes/config.yaml` in the route home before the launcher runs (Hermes defaults that
switch to true). An existing `config.yaml` is not read and refuses
`hermes_config_blocks_adopt_override`. `borrow_claude_code_login` is not a profile field.

## Tests

From the repository root (Python 3.11+, standard library only):

```sh
python3 -B -m unittest discover -s core/recursion_core/subscription_routes/tests -t core -v
```

(`-t core` puts `core/` on the import path so `recursion_core` imports; `-t .` fails with
`ModuleNotFoundError`.) `tools/ci_gate.py` does not discover `core/` tests; they run only when started by hand.

## Evidence

Governing spec: csl-research `contracts/PRODUCT.md` at
`055cc7a5c793b7135e5d0565dc4198cf96230531`, SHA-256
`e64783152f6d67d4ede774dc41d112fe46c2d7536d53cb2c04e71a3566044daf`, CSLR-D-011.

Candidate record this module was written against (research stays in the vault):

- Repository: `CSL-Laboratories/csl-research`
- Commit: `3b9a8469a3bd10ba22b7875b08da427144dcee09`
- Path: `submissions/20261107-csl786-subscription-routes-01/`
- Claim: Pi, Hermes and OpenCode refuse a Claude-subscription route before spawn, and the
  2026-11-08 sign-in allowlist matches PRODUCT.md sections 2 and 3.3.
- Limitations: candidate evidence, not founder acceptance. This VM could not re-read
  csl-research (GitHub HTTP 404). `3b9a8469a3bd10ba22b7875b08da427144dcee09` is the last
  full SHA this module's earlier notes recorded (CSL-786, 2026-11-09). A later comment on
  the same issue (2026-11-09T21:54:27Z) names csl-research #1099 head
  `8592d47062153875ca36f9fbe5b4228f0c70bdf6`; that tree was not opened here. The old
  pointer at branch `grounding-1077` is not a pin and is not cited as one.
