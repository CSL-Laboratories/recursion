# keys: user-entered API keys

Recursion keeps the model-provider API keys a user types in. It keeps them in the OS keychain where there is one, and in an owner-only file on a host with none. A key is never logged, never put in a prompt and never sent to CSL. Engine sign-ins (Claude Code, Codex) stay in each engine's own store and are never read here. Any vendor's key can be kept. This package is the store, its commands and the credential source that lets an API-key route run.

## Commands

The same four actions are available as `recursion-control key ...` (once SPINE's seams land) and as `python -m recursion_core.keys ...` (which needs no daemon):

| Command | What it does |
|---|---|
| `add ID --provider VENDOR --env VARIABLE` | Reads the key from the terminal with echo off. It asks whether runs on this key may spend money billed to it, and the cap in US dollars. `--stdin` reads the key from standard input instead, and only when that is not a terminal; `--allow-metered-spend --cap-usd N` answers the spend questions up front. A key is never read from the command line. |
| `list [--json]` | Ids, providers, variable names, save times, the store, the spend opt-in, and any unfinished operation. It never shows a value. |
| `remove ID [--yes]` | Asks first (`--yes` skips the question), then deletes the key and says from where. It also clears what an interrupted save left behind. |
| `audit [--path P] [--no-journal]` | Looks for every saved value in Recursion's state and config folders, the given paths and the user journal. It names each place a value was found, never the value, and any temp file an interrupted save left in the store's folder. It exits 1 when one was found, 4 when the search was incomplete (a value or a file could not be read, an operation is unfinished, or the journal was asked for and not searched to the end), and 3 when it was refused. |

- **`ID`** is the route's connection id (`route.connection.id` in `routes.json`). It must start with a letter or digit, be at most 64 characters, and not look like a key: no word (letters and digits between `.`, `_`, `:` and `-`) longer than 16 characters, none of more than 11 that mixes letters and digits, no more than 8 digits in a row, and no more than 4 changes between lower case, upper case and digits inside its words. `codex-key`, `login-B`, `gpt4o-mini` and `claude-3-5-sonnet-20241022` are ids; `AKIAIOSFODNN7EXAMPLE`, 32 hex digits, `abcdefgh12345678` and `sk-ant-api03-Ab3d-Ef5h` are refused, without being repeated. A key of one case of letters only, in words of at most 16, cannot be told from a name and is accepted.
- **`VARIABLE`** is the variable the engine reads the key from: `ANTHROPIC_API_KEY` for Claude Code, `CODEX_API_KEY` or `OPENAI_API_KEY` for Codex.
- **`VENDOR`** is any lower-case vendor id: 1 to 64 of `a-z 0-9 . _ -`, starting with a letter or digit. Eight are published, one per vendor whose API key the product contract names, and lane ROUTING routes those: `anthropic`, `openai`, `google`, `xai`, `cursor`, `opencode`, `nous` and `refuseless`. The list, with the contract line behind each id, is `PROVIDERS` in `validate.py`. A key saved for any other vendor is kept and listed, but it is handed to no engine until its vendor is published (`provider_unrouted`). No route or grant can name a vendor yet, so nothing could tell that a route is meant for that key.

Anything else typed after `key` is refused and not repeated, so a key pasted on the command line by mistake does not come back in an error.

## Where keys are kept

The index, `$XDG_CONFIG_HOME/recursion/keys/index.json` (default `~/.config/recursion/keys/`), holds the metadata and never a value. The values are in the backend:

- **Linux with a Secret Service on the session bus:** the Secret Service, through `secret-tool`. The key goes in on its standard input and comes back on its standard output, never in its arguments or environment. Without `secret-tool` (package `libsecret-tools`), the store refuses rather than write a file. KWallet is used through its Secret Service interface; with that interface off, the store refuses rather than write a file. A removal counts as done only when `secret-tool lookup` finds nothing and `secret-tool search --all` (which lists locked items too) lists nothing; otherwise it is refused and the key stays listed.
- **No session bus, or a bus with no keychain on it:** the owner-only file `secrets.json` beside the index.
- **macOS and Windows:** their keychain backends are not built yet, so the store refuses there instead of writing a file.

**Files.** The folder is 0700 and every file in it is 0600, owned by the user. All of this is checked on every open, through the descriptor that was opened. Symlinks are refused and not followed, and a FIFO cannot block a read. Writes go through a temp file, fsync and rename, with the folder's lock held exclusively. Reads hold it shared, so a read never sees a key change halfway (see `files.py`). The index holds at most 1 MiB and the owner-only file 4 MiB; a save that would exceed either is refused (`store_full`) before anything changes. A temp file left by an interrupted write is deleted by the next save or removal.

**Identity.** A store is its folder, however the folder is spelled. Its id, which names its items in the keychain, is random and recorded in the index at the first save, so every path that reaches the folder (`..`, a symlinked parent, a trailing slash, a bind mount, another letter case where the file system ignores case) opens the same index and reaches the same values; a relative folder is refused.

**Binding.** Each save also gets a random id of its own. The value is stored with the store's id, that save id and the record's provider: as attributes of the keychain item, or beside it in the owner-only file, whose header also names the store. A value is handed out only when all three match the record being used. So an older `index.json` put back, an index copied in from elsewhere, or an edited record gets `secret_missing`, never another save's value. An owner-only file without these ids, or naming another store, is refused. A removal deletes every value saved for the id in the store.

**Pinning.** The first save records the backend the host offered then. Every later operation that touches a value refuses when the host now offers a different one: `keychain_unavailable` when the keychain is gone, `backend_changed` otherwise. A key pinned to a keychain is removed where that keychain answers, since its removal must reach the value. A key pinned to the owner-only file can always be removed, so once a keychain appears, keys are moved by removing them and adding them again. A locked keychain is refused (`keychain_locked`) and is never answered from the file. The pin is released when nothing is left.

**Unfinished operations.** An add or a removal records its intent in the index before it touches a keychain or the file. If the outcome is not certain (the keychain helper was killed or ran out of time and the keychain cannot say it holds nothing, or the process was killed), the intent stays:
- `list` shows it;
- the key is refused (`operation_pending`), whether or not its add had finished;
- the next save or removal finishes or undoes it;
- `remove ID` clears it on request.

## The credential source

`credentials(connection_id, env_name)`, imported as `from recursion_core.keys import credentials`, is the `Credentials` type of `routes/base.py`. It returns the value only when the id is saved under exactly that variable name and the key has a spend opt-in with a cap above 0. An unknown id, or a known one asked for under another name, raises `KeyError(connection_id)`, as the daemon's default does. A refused known id, or one whose add or removal is unfinished, raises `KeyRefused`, a `KeyError` with a typed code. Either way the route refuses before launch: `launch_refused`, "no credential for ID". It is the only way a value leaves the store for a route; `KeyStore.values()` serves `audit` alone.

`hooks.py` registers it into seam S7, and the `key` command family into S2, once SPINE's seams pull request lands. Until then the daemon still passes `no_custody` and every API-key route refuses, as before.

## Spend

Each key records `metered_spend_allowed` and `cap_usd`, entered at `add`. With no opt-in the cap is 0, and every run on the key is refused before launch until it is removed and added again with one. Lane QUOTA enforces the cap; until it does, the provider console's spend limit is the hard stop.

## Not in this package yet

The macOS Keychain backend, `key.list` (S1), `describe()` and `spend_policy()` (pull request 2); the Windows Credential Manager backend, replace and rotate (pull request 3). Import from other tools is lane IMPORT's.
