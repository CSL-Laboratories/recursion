# keys: user-entered API keys

Recursion keeps the model-provider API keys a user types in. It keeps them in the OS keychain where there is one, and in an owner-only file on a host with none. A key is never logged, never put in a prompt and never sent to CSL. Engine sign-ins (Claude Code, Codex) stay in each engine's own store and are never read here. Any vendor's key can be kept. This package is the store, its commands and the credential source that lets an API-key route run.

## Commands

The same four actions are available as `recursion-control key ...` (once SPINE's seams land) and as `python -m recursion_core.keys ...` (which needs no daemon):

| Command | What it does |
|---|---|
| `add ID --provider VENDOR --env VARIABLE` | Reads the key from the terminal with echo off. It asks whether runs on this key may spend money billed to it, and the cap in US dollars. `--stdin` reads the key from standard input instead, and only when that is not a terminal; `--allow-metered-spend --cap-usd N` answers the spend questions up front. A key is never read from the command line. |
| `list [--json]` | Ids, providers, variable names, save times, the store and the spend opt-in. It never shows a value. |
| `remove ID [--yes]` | Asks first (`--yes` skips the question), then deletes the key and says from where. |
| `audit [--path P] [--no-journal]` | Looks for every saved value in Recursion's state and config folders, the given paths and the user journal. It names each place a value was found, never the value. |

`ID` is the route's connection id (`route.connection.id` in `routes.json`). `VARIABLE` is the variable the engine reads the key from: `ANTHROPIC_API_KEY` for Claude Code, `CODEX_API_KEY` or `OPENAI_API_KEY` for Codex. `VENDOR` is one of `anthropic`, `openai`, `google`, `xai`, `cursor`, `opencode`, `nous` and `refuseless`: one id per vendor whose API key the product contract names. The list, with the contract line behind each id, is `PROVIDERS` in `validate.py`, and lane ROUTING reads its vendor ids from it.

## Where keys are kept

The index, `$XDG_CONFIG_HOME/recursion/keys/index.json` (default `~/.config/recursion/keys/`), holds the metadata and never a value. The values are in the backend:

- **Linux with a Secret Service on the session bus:** the Secret Service, through `secret-tool`. The key goes in on its standard input and comes back on its standard output, never in its arguments or environment. Without `secret-tool` (package `libsecret-tools`), the store refuses rather than write a file.
- **No session bus, or a bus with no Secret Service:** the owner-only file `secrets.json` beside the index.
- **macOS:** the login keychain, through `/usr/bin/security`. The key is written with `security -i` from standard input, as hexadecimal, never in the arguments, and read back to check it was kept exactly. This backend follows Apple's documented interface and is tested against a stand-in program; it has not yet run on a Mac.
- **Windows:** the Credential Manager backend is not built yet, so the store refuses there instead of writing a file.

The folder is 0700 and every file in it is 0600, owned by the user. All of this is checked on every open, through the descriptor that was opened. Symlinks are refused and not followed, a FIFO cannot block a read, and writes go through a temp file, fsync and rename under a lock (see `files.py`).

**Pinning.** The first save records the backend the host offered then. Every later operation that touches a value refuses when the host now offers a different one: `keychain_unavailable` when the keychain is gone, `backend_changed` otherwise. A locked keychain is refused (`keychain_locked`) and is never answered from the file. The pin is released when no key is left.

## The credential source

`credentials(connection_id, env_name)` is the `Credentials` type of `routes/base.py`. It returns the value only when the id is saved under exactly that variable name and the key has a spend opt-in with a cap above 0. An unknown id, or a known one asked for under another name, raises `KeyError(connection_id)`, as the daemon's default does. A refused known id raises `KeyRefused`, a `KeyError` with a typed code. Either way the route refuses before launch: `launch_refused`, "no credential for ID".

`hooks.py` registers it into seam S7, `key.list` into the method table (S1) and the `key` command family into S2, once SPINE's seams pull request lands. Until then the daemon still passes `no_custody` and every API-key route refuses, as before.

## For other lanes

- **`key.list`** (S1, a read) answers `{"code": "ok", "keys": [...]}`, each key with exactly `key_id`, `provider`, `env_name`, `created_at` and `store`, as published in `schemas/control/v1/methods.v1.json`. It reads the index only, so no keychain is asked. `tests/fixtures/key.list.ok.json` is an example answer.
- **`spend_policy(connection_id)`** returns `{"metered_spend_allowed": bool, "cap_usd": number}`, read from the index. This is what lane QUOTA reserves against.
- **`describe()`** returns the folder, the pinned store, the backend the host offers now (or why it cannot say), the owner-only file's path when it is in use, and the key count. This is what lane INSTALL's `doctor` reports. It never raises for a store it cannot use, and it never returns a value.

## Spend

Each key records `metered_spend_allowed` and `cap_usd`, entered at `add`. With no opt-in the cap is 0 and every run on the key is refused before launch. Lane QUOTA enforces the cap; until it does, the provider console's spend limit is the hard stop.

## Not in this package yet

The Windows Credential Manager backend, replace and rotate (pull request 3). Import from other tools is lane IMPORT's.
