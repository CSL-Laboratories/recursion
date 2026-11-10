# core/recursion_core/subscription_routes (CSL-786, candidate)

Refuses Claude-subscription routes in Pi, Hermes and OpenCode engine profiles before any
spawn. Under Q12 as widened 2026-11-08 (PRODUCT.md `055cc7a5`, CSLR-D-011, sections 2 and 3.3) a
subscription runs through another harness's own sign-in only where that provider allows that
harness; Claude-subscription sign-ins stay refused in these three, and an Anthropic API key
still works. Candidate code: not yet called by a running core, and not
reviewed or accepted.

- `check_route(engine, profile, child_env, route_home=..., parent_env=..., operator_home=..., wrapper=...,
  requirements=..., full_access_acknowledged=...)`
  validates one `pi`, `hermes` or `opencode` profile and the exact child environment, and
  returns a `RouteDecision` or raises `SubscriptionRouteRefused(engine, cause, detail)`.
  Pi and Hermes require `wrapper` (see Wrapper pins). Cursor and Antigravity CLI use
  `check_cursor_route` and `check_antigravity_route`.
- `guard_then_spawn(launcher, argv, ...)` calls `check_route` and only then the launcher,
  with `shell=False` and the checked env rebuilt by `child_env.build_child_env` (a temp name
  becomes one private temp root; the one checked credential is kept under the harness's own
  name). That env is never `os.environ`. It refuses `env`, `shell`, `executable`,
  `preexec_fn`, fd, user and group options before any check, forwards `cwd`, and refuses
  any other option (see Launch options). `launcher` and `argv` are trusted, core-owned
  inputs: argv is not parsed beyond `argv[0]` and the model, so it must not carry a
  provider, config or login flag the profile did not choose. `guard_cursor_then_spawn` and
  `guard_antigravity_then_spawn` follow the same launch rules.
- `requirements` (a `RouteRequirements`) and `full_access_acknowledged` (a `bool`) are the
  only way a user's or plugin's configuration reaches these functions (see Configured
  requirements and The full-access profile). The defaults are the secure ones.

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
  hold none of these at spawn. Checked with `lstat` only; no credential file is opened.
  One exception, for Hermes routes only: a `.hermes` directory that holds nothing but the
  `config.yaml` an earlier launch in this home wrote, or (API-key Hermes route) nothing at
  all (see Hermes route-home reuse).

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
  removed from that run's child, as is every other provider credential (`*_API_KEY`, `*_TOKEN`,
  `GITHUB_PAT`, `GH_PAT`, `GITHUB_PASSWORD`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
  `OPENAI_BASE_URL`, `OPENAI_API_BASE`, for example `ANTHROPIC_API_KEY`) and git's
  config-injection channel (`GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_*`, `GIT_CONFIG_VALUE_*`,
  `GIT_CONFIG_PARAMETERS`). Anything outside `child_env`'s base allowlist is removed too, and a
  proxy value with userinfo is removed. What remains is rebuilt with `build_child_env` and started
  by `spawn_copilot_own_loop`. An optional `model` is checked (printable, no spaces, at most 128
  characters) and carried on the decision. With no stored `copilot` login, no probe, a probe that
  fails or raises `SystemExit`, or a probe that does not return within 5 seconds
  (`LOGIN_PROBE_DEADLINE_S`; a caller may pass a shorter `probe_deadline`, not a longer one) the
  run refuses rather than fall back to `gh`. On POSIX that late probe is SIGKILLed with its
  process group, so it and children that stayed in the group do not keep running. The probe is
  the caller's; nothing here opens a login store.
- **Nous, "Local proxy"**: `check_nous_own_loop(route, parent_env, ...)` with
  `{"engine": "nous", "auth": "subscription", "method": "hermes_local_proxy", "base_url": ...}`.
  The own loop calls only the user's own Hermes subscription proxy at a loopback IP literal
  (`127.0.0.0/8`, `::1`, or IPv4-mapped loopback); names, including `localhost`, are not resolved
  and refuse. Adapters are `["nous"]` only; `xai` refuses (`adapter_must_stay_off`). Recursion holds
  no Nous token: token-like route fields and URL credentials refuse. A query string or fragment
  on the proxy URL is treated as a credential, same as userinfo, and the same provider
  credentials as on the Copilot child are removed. Proxy settings (`HTTP_PROXY`, `HTTPS_PROXY`,
  `ALL_PROXY`, any case) are removed too, so loopback-bound traffic cannot be carried off the host.
  Names outside `child_env`'s base allowlist are removed as well. `spawn_nous_own_loop` rebuilds
  what remains with `build_child_env` and starts the process. An optional `model` is checked and
  carried; an adapter-prefixed id must name `nous` (`xai/grok-3` refuses
  `adapter_must_stay_off`).
- `nous_proxy_doctor(base_url, current_user=..., interactive_users=[...])` reports the address,
  refuses one that is not loopback, and warns when other interactive users share the host (any
  local user can reach a loopback proxy, which "accepts any bearer"). The reported address and
  the refusal text leave out URL userinfo, query and fragment, so `ftp://user:secret@...`,
  `http://user:secret@...` and `http://127.0.0.1/?token=secret#secret` do not echo that secret.
  With no `interactive_users`, it and `check_nous_own_loop` warn "host users unknown" instead of
  reporting a clean host.
- Still *[to verify]* in PRODUCT.md (O61): GitHub's Generative AI Services terms for Copilot, and
  whether the Nous Portal terms' consent clause covers an app calling through the proxy. No
  provider, DNS or network call is made here.

## Antigravity CLI is the Google route; the Gemini CLI route was dropped

Owner call (Lucas, 2026-11-09): the Gemini CLI route is dropped entirely and Antigravity CLI
(`agy`) replaces it. `gemini_cli.py`, `check_gemini_cli_sign_in` and `GeminiCliRefused` are
gone: importing `recursion_core.subscription_routes.gemini_cli` raises `ModuleNotFoundError`,
and `gemini` is not an engine (`check_route("gemini", ...)` refuses `unknown_engine`). There
is no Gemini API-key route here either. Background: Google's
[Gemini Code Assist consumer accounts](https://developers.google.com/gemini-code-assist/docs/deprecations/code-assist-individuals)
note says Gemini CLI stopped serving consumer accounts on 2026-06-18, and its
[transition post](https://developers.googleblog.com/an-important-update-transitioning-gemini-cli-to-antigravity-cli/)
names Antigravity CLI as the subscription path.

Nothing else about Google changed. A Google or Gemini sign-in label in a Pi, Hermes or
OpenCode profile (`google*`, `gemini*`, `antigravity*`) still refuses
`google_subscription_refused`, and a `gemini` API-key provider still refuses
`provider_not_allowed`.

`check_antigravity_route` is the beta wrapper for `agy`. Its pin is the version plus the
sha256 of the `agy` binary and of its launcher. Package files are not part of that pin.
A mismatch refuses `wrapper_pin_mismatch` until the pin is replaced. The binary is not run.
T3 Code is background only: this package has no T3 Code route.

The floor table's Measured help column (which builds have a measured `--help`) already
landed on recursion #323. It is not repeated here.

## Wrapper pins (Pi, Hermes, Cursor)

Lucas, 2026-11-09: Pi, Hermes and Cursor run as beta when the launcher script and the
installed package files match a pin. `measure_package_pin(launcher_path, install_root, *,
allowed_link_targets=(), full_access_acknowledged=False)` hashes the launcher and every file
under the install root and returns a `PackagePin`. Pass it in a `WrapperInstall` as
`wrapper=` to `check_route` (Pi, Hermes) or `check_cursor_route`. A changed, extra or
missing file, or a different launcher, refuses `wrapper_pin_mismatch`. OpenCode is not in
this pin. Cursor's child env carries no credential. A missing pin on Pi or Hermes refuses
`wrapper_pin_missing`.

The rules below hold both when a pin is measured and when it is verified
(`measure_package_pin` raises `WrapperPinRefused(cause, detail)`; the route checks raise
`SubscriptionRouteRefused` with the same cause).

Symlinks (owner call 1):

- A symlink is followed and what it resolves to is hashed. A file link's row covers the
  target's bytes. A directory link's tree is walked and every regular file under it gets
  its own row (`node_modules/dep/index.js`). So a pnpm-style `node_modules` link stops
  matching when the code behind it changes.
- The link itself is in the manifest too (its text and where it resolves), so pointing a
  link at other content changes the pin even when the bytes are the same.
- A link whose real path leaves the real install root refuses `package_link_outside_root`.
  It is followed only when the caller lists the target in `allowed_link_targets`, or the
  run uses the full-access profile. Its contents are hashed either way, and a link inside
  an allowed tree may not leave that tree.
- A dangling link or a link loop refuses `package_link_dangling`, with or without full
  access.
- A directory link back to a directory already being walked (a workspace self-link) is
  recorded and not entered again; that directory's files are already in the manifest.
- The launcher is hashed by its resolved contents.

Pin shape (owner call 2):

- `install_root` must be an absolute path: `install_root_not_absolute`.
- The manifest must hold at least one file: `package_manifest_empty`. An empty install
  directory is not a pin, and a hand-built `PackagePin` with no files refuses too.
- The launcher's real path must sit under the real install root: `launcher_outside_root`.
  A shim link elsewhere that resolves into the install root is fine. Full access does not
  relax this.
- Other causes: `wrapper_install_missing`, `wrapper_launcher_missing`,
  `wrapper_package_not_a_file`, `wrapper_file_too_large`, `wrapper_tree_too_large`,
  `wrapper_tree_too_deep`, `wrapper_tree_unreadable`, `wrapper_pin_malformed`.

## Launch binding

`argv[0]` (owner call 3). On a pinned route `argv[0]` must be an absolute path whose
`os.path.realpath` equals the `realpath` of the pinned path; anything else refuses
`argv0_not_pinned` before the launcher is called:

| Guard | `argv[0]` must resolve to |
|---|---|
| `guard_then_spawn`, Pi and Hermes | `wrapper.launcher_path` |
| `guard_cursor_then_spawn` | `wrapper.launcher_path` |
| `guard_antigravity_then_spawn` | `binary_path` or `launcher_path` (both are in the Antigravity pin) |

A PATH name such as `cursor`, a relative path, or another binary refuses. A link to the
pinned file passes, and argv reaches the launcher unchanged. OpenCode has no pin here, so
`guard_then_spawn` does not bind its `argv[0]`. Full access does not relax this. The check
compares paths at guard time; it does not hold the file open across the launch.

`guard_then_spawn` launches the env `check_route` accepted, rebuilt through `child_env`
(`validated_launch.launch_env`), not a second read of the caller's mapping and not
`os.environ`. `spawn_route_process` is the subprocess entry for that same env.
A subscription `model` must appear in argv, or every `--model`, `--model=` and `-m`
before `--` must name that same model. A second flag with a different value refuses
`model_not_bound` (the first match does not bind). An empty model segment (`anthropic/`)
refuses `model_name_empty`. `borrow_claude_code_login` is not a profile field.
`repr(RouteDecision)` leaves out `child_env`, which holds the API key on an API-key route.

### Launch options

Owner call 5. Keyword options passed to a guard are sorted into three groups:

- Forbidden, always `launch_option_denied`: `env`, `shell`, `executable`, `preexec_fn`,
  `close_fds`, `pass_fds`, `user`, `group`, `extra_groups`, `cwd_fd`, plus `engine_config`
  and `model`, which the guard itself passes to the launcher.
- Forwarded to the launcher unchanged: `cwd` (`FORWARDED_LAUNCH_OPTIONS`), plus any name the
  caller lists in `RouteRequirements.extra_launch_options`.
- Everything else refuses `launch_option_unknown`, under the full-access profile too.
  Nothing is silently dropped.

### Hermes route-home reuse

Owner call 4. Hermes's `auth.adopt_external_logins: false` is written to
`<route_home>/.hermes/config.yaml` before the launcher runs (Hermes defaults that switch to
true). A second launch in the same home works:

- If `config.yaml` already exists it is reused only when its bytes are exactly
  `auth:\n  adopt_external_logins: false\n` (37 bytes). Any other content, a symlink, a
  non-regular file or another owner refuses `hermes_config_blocks_adopt_override`.
  `check_route` applies the same rule, so it agrees with the guard.
- A config of any other size is refused from `lstat` alone and never opened. That includes
  a zero-length or short `config.yaml`: it is not adopted or completed. Only a file already
  exactly 37 bytes is read, to compare it. The read opens `.hermes` as a real directory
  (`O_DIRECTORY|O_NOFOLLOW`), opens `config.yaml` relative to it with
  `O_NOFOLLOW|O_NONBLOCK|O_NOCTTY`, and checks type, size and owner again on the opened
  file.
- On an API-key Hermes route, a `.hermes` directory that holds only that exact file is
  accepted, and an empty real `.hermes` directory owned by the effective user counts as
  absent. Anything else there (another file, a link) still refuses
  `engine_store_in_route_home`. Pi, OpenCode, Cursor and Antigravity routes still refuse
  any `.hermes`, empty or not.
- On a sign-in route `.hermes` is Hermes's own store and may hold its login as before. When
  it holds only Recursion's switch, the `own_login_store_not_inspected:.hermes` flag is not
  set, since nothing in it is uninspected.

How the switch is written:

- `.hermes` absent, or an empty real directory: the guard makes
  `<route_home>/.hermes.recursion-<random>` (mode 0700), creates `config.yaml` in it
  (`O_CREAT|O_EXCL|O_NOFOLLOW`, mode 0600), writes the 37 bytes, syncs the file and the
  staging directory, renames the staging directory onto `.hermes` and syncs the route
  home. A directory sync that fails is ignored. If the rename fails with `ENOTEMPTY` or
  `EEXIST` (something appeared in `.hermes` meanwhile) the launch refuses
  `hermes_config_blocks_adopt_override` and the staging directory is removed.
- `.hermes` holds entries and no `config.yaml` (a sign-in route's own store): the 37 bytes
  go to a `.hermes.recursion-<random>` temp file in the route home, which is hard-linked to
  `.hermes/config.yaml` and then removed. A link never overwrites and does not follow a source
  symlink. Any failure on this path refuses `hermes_config_blocks_adopt_override`.
- After either write the guard reads the switch back with the same rules and refuses to launch
  if it is not exactly that regular file. A symlink left at `config.yaml` is removed.
- Fallback for any other leftover (a partial or foreign `config.yaml`, a `.hermes` this
  route refuses): the driver discards the route home and makes a new one. The guard does
  not repair it.
- A leftover `.hermes.recursion-*` entry (a crash between the two steps) is not a store
  entry: no check reads it and it does not refuse a launch. The next launch may remove
  it. This package does not remove one today; the driver can, or it goes with the route
  home.

## Configured requirements

Secure defaults first. What a user or a plugin configures comes in as explicit inputs to
the check and guard functions. This package reads no file, environment variable or keyring
for them and never prompts: the driver owns prompts and saved configuration, and passes
the saved values in on each run. These are Lucas's 2026-11-09 owner calls on recursion #321,
under CSLR-D-011 and PRODUCT.md `055cc7a5c793b7135e5d0565dc4198cf96230531` (SHA-256
`e64783152f6d67d4ede774dc41d112fe46c2d7536d53cb2c04e71a3566044daf`; "the user should be
able to customize this or add plugins to configure this", grill Q20).

`RouteRequirements` (frozen dataclass), passed as `requirements=`; `None` means the
defaults:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `allowed_link_targets` | `tuple[str, ...]` or `frozenset[str]` | `()` | Absolute, normalised real paths a package pin may follow a link to outside the install root (the path or anything under it). `/` is not accepted. |
| `extra_launch_options` | `tuple[str, ...]` or `frozenset[str]` | `()` | Launch option names forwarded besides `cwd`. Each must be an identifier. |
| `owned_hermes_home` | `str` or `None` | `None` | The Hermes home the user says they own. When set it must resolve to the route home, or a Hermes route refuses `hermes_home_not_owned`. `None` means the route home Recursion created. Ignored on other engines. |

- Any other container or entry type (a list, a bare string, a relative path, a non-string)
  refuses `requirements_malformed` with the field name as detail. So does a `requirements`
  value that is not a `RouteRequirements`.
- A forbidden or reserved launch option in `extra_launch_options` refuses
  `requirements_forbidden_launch_option`. No input can make `env`, `shell`, `executable`
  or `preexec_fn` forwardable.
- `owned_hermes_home` grants nothing beyond the default: `.hermes/config.yaml` is written
  or reused only in the route home, and the route-home rules (private, not the operator's
  home) still apply to it.
- `measure_package_pin` takes `allowed_link_targets` and `full_access_acknowledged` directly,
  with the same validation.

## The full-access profile

`full_access_acknowledged: bool = False` on `check_route`, `check_cursor_route`,
`check_antigravity_route`, the three guards and `measure_package_pin`. It means this run
uses the named full-access profile and the user acknowledged it for this run (CSL-460's
full-access mode; PRODUCT.md `055cc7a5` section 3.3, grill Q20, O44, H-06). The name and
meaning match `child_env`'s `ChildRoute.full_access_acknowledged`.

- Only the bool `True` counts. Any value that is not a bool (`"true"`, `1`, `None`) refuses
  `route_full_access_malformed`.
- It is never inferred. No parent environment variable, child environment variable,
  profile field or override turns it on.
- This package renders no UI. The driver must show the danger notice when the named
  full-access profile is turned on, and only then pass `True` for that run.
- A decision made under it carries the flag `full_access_profile_acknowledged`.

What it relaxes:

- a package link may resolve outside the install root (its contents are still hashed and
  must still match the pin). Nothing else.

What it never relaxes:

- every Claude-subscription, Google and unconfirmed sign-in refusal, and every child-env,
  credential and route-home rule;
- the forbidden launch options (`launch_option_denied`,
  `requirements_forbidden_launch_option`);
- the forwarded launch options: still `cwd` plus the names in
  `RouteRequirements.extra_launch_options`. Any other name still refuses
  `launch_option_unknown`, so a driver that wants `start_new_session` or the like lists it
  in `extra_launch_options`;
- the pin itself (`wrapper_pin_mismatch`, `package_manifest_empty`,
  `install_root_not_absolute`, `launcher_outside_root`, `package_link_dangling`) and the
  `argv[0]` binding (`argv0_not_pinned`);
- Hermes's `auth.adopt_external_logins: false` and the `config.yaml` rule.

## Refusal codes added on 2026-11-09

| Code | When |
|---|---|
| `package_link_outside_root` | a package link resolves outside the install root and was not allowed |
| `package_link_dangling` | a package link resolves to nothing, or loops |
| `package_manifest_empty` | the install root holds no file, or the pin lists none |
| `install_root_not_absolute` | `install_root` is not an absolute path |
| `launcher_outside_root` | the launcher's real path is not under the install root |
| `wrapper_tree_too_deep`, `wrapper_tree_unreadable` | the walk went past 200 levels, or a directory could not be listed |
| `argv0_not_pinned` | `argv[0]` does not resolve to the pinned path |
| `launch_option_unknown` | a launch option that is neither forbidden, built in nor configured |
| `requirements_malformed` | a `RouteRequirements` field (or the value itself) has the wrong type or shape |
| `requirements_forbidden_launch_option` | `extra_launch_options` names a forbidden or reserved option |
| `hermes_home_not_owned` | `owned_hermes_home` does not resolve to the route home |
| `route_full_access_malformed` | `full_access_acknowledged` is not a bool |

`launch_option_denied` now also covers `engine_config` and `model`.
`hermes_config_blocks_adopt_override` now means a `config.yaml` that is not exactly
Recursion's own, or a switch that could not be put in place without replacing something
(the staging rename found `.hermes` filled, or the link into `.hermes` failed). No refusal text or `repr` here carries a key, a token or an env value.

## Tests

From the repository root (Python 3.11+, standard library only):

```sh
python3 -B -m unittest discover -s core/recursion_core/subscription_routes/tests -t core -v
```

(`-t core` puts `core/` on the import path so `recursion_core` imports; `-t .` fails with
`ModuleNotFoundError`.) `tools/ci_gate.py` also runs each `core/**/test_*.py` file with `core/`
on `PYTHONPATH` (CSL-795). `tests/test_owner_calls.py` holds the tests for the 2026-11-09
owner calls described above.

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
