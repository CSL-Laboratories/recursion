# Plugin records, version 1 (drafts)

The records a plugin package and its install share with the core. Each is a JSON Schema (Draft
2020-12) with fixtures, checked by the client in `conformance/`. Every schema here is tagged
`1.0-draft.N`; a draft may still change, and `CHANGES.md` lists each change.

| File | Record | What it is for | Who must send a fixture |
|---|---|---|---|
| `manifest.v1.json` | `csl-plugin-manifest/1` | The manifest a developer signs: the ten fields of the present manifest, plus `runtime`, `interface` and `files` (every file of the package with its SHA-256) | PLUGIN-SIGN, PLUGIN-HOST, PAGE |
| `install.v1.json` | `install.json` | This install's trusted keys, grants, and the registry label the installer recorded for each manifest's bytes, exactly as the daemon reads the file today | PLUGIN-SIGN, PLUGIN-HOST, INSTALL |
| `api.v1.json` | `csl-plugin-api/1` | The plugin API's entry points as plain data. A skeleton: the shapes are for the lanes that host plugins and draw the page to supply | PLUGIN-HOST, PAGE |
| `revoked-keys.v1.json` | `csl-revoked-keys/1` | The signed list of revoked developer keys. A draft: revocation's details are open | PLUGIN-SIGN |
| `host.v1.json` | `csl-plugin-host/1` | The messages on the channel between the daemon and a plugin host, one channel per loaded plugin | PLUGIN-HOST: the daemon's channel end and the plugin host program |

## What holds for the plugin API whatever its shapes become

`api.v1.json` states these, and its fixtures check them:

- Every call a plugin makes into the core carries the principal `{kind: plugin, id}`, set by the host
  and never by the plugin.
- `on_event` names the event types it subscribes to, receives engine output only as untrusted text,
  and never receives an approval's private binding.
- A plugin's command or tool is reachable only through the one dispatcher, `plugin.call`.
- A run a plugin owns has that plugin as its owner, set by the host, and its effects pass the same
  approval gate as any other.
- A theme or a layout never reaches the approval area.

## Rules a schema cannot say

The parser enforces these and a validator cannot. A client that only validates must not treat a
record as accepted on the schema's word alone.

Manifest:
- `permissions` are listed in the order `manifest.v1.json` lists them, with no repeats.
- `network_destinations` and `credential_refs` are sorted.
- `files` contains the entry file, and its digest is `artifact_sha256`.
- The bytes are in the restricted canonical form (ASCII, sorted keys, whole numbers, no spaces),
  and the signature is over those bytes.
- No grant may name a destination or a credential name that an effect owner reserved.

Install file:
- No two grants name the same `plugin_id`.
- The file is a regular file of the operator's that no one else can write.
- A registry label applies only when its key is the SHA-256 of the installed `manifest.json` bytes.
  A key for any other bytes is not this manifest's label, and the daemon shows the plugin unlisted.
  The label is not a permission and not a grant.

## Loader admission

To bind a hand-made grant, set `developer_key_id` in its `install.json` grant entry.
A manual grant with no developer key binding is refused before plugin code runs if the
manifest requests permissions, whether the manifest is signed or not. A plugin that asks for no
permissions can still load. `order_after_revoked_key` is null or a developer key ID, the same shape
as `developer_key_id`; any other value is refused.

For a signed version 1 load, every listed path must name a file in the package. The package folder
itself and every folder on a listed path must be a real folder: no symbolic link is followed. Before
any plugin code runs, the loader then reads every listed file: it and every folder on its path must
belong to the user the daemon runs as and must not be writable by a group or other users
(`package_file_untrusted`), and its SHA-256 must be the one the manifest lists
(`package_file_digest_mismatch`). Nothing else may stand in the package folder or in a folder on a
listed path (`package_file_unlisted`): only the listed files and, at the top, `manifest.json` and
`manifest.sig`. A `__pycache__` folder is not exempt, because Python would import its compiled files
in place of the checked source; once it loads from a package folder, the loader turns off the
interpreter's writing of compiled files. For the same reason the load is refused
(`package_compiled_file_prefix`) when, at the moment of the check, the interpreter keeps its
compiled files outside the package, under a prefix set by `PYTHONPYCACHEPREFIX`, by
`-X pycache_prefix` or in the process: Python would import a compiled file from there in place of a
checked listed file, and it checks nothing of who can write that folder. The loader looks at the
prefix (`sys.pycache_prefix`) once per load, after the package checks and before any plugin code
runs; Python reads it again at every import the plugin makes, during the load and for as long as
the plugin runs. Each refusal names the path, or the prefix.

So the signature covers the whole package at the moment of the check, and that is where it stops.
Three cases still run code the signature does not cover. First, the loader runs only `plugin.py`
from the bytes it checked; Python reads every other file again when the plugin imports it, after the
check, and a file changed in between (only a writer with the daemon user's own rights can) runs as
it is then. Second, Python hands the plugin a module of the same name that it finds before the
listed file, and never reads the listed file: one the daemon's process already holds (imported
earlier by another plugin or by the daemon itself), one built into or frozen in the interpreter, or
one in a folder that comes before the package folder on the import path (where the plugin's own code
puts its folder after the others). Third, code already running in the daemon's process (the
daemon's own start, a site file of the interpreter, or a plugin loaded earlier, signed or not and
with no grant) can install a finder or import hook, or set a compiled-file prefix after the loader
has looked at it, and Python then hands the plugin other code for a listed module's name. That code
runs under the plugin's signed name and grants, and the plugin's record still says every listed file
was checked. No check in the loader can hold against it: such code runs with the daemon's rights and
could as well change the loader itself. In this build every plugin runs in the daemon's own process,
so one plugin can run its code under another plugin's signed name and grants until plugins are
contained. An unsigned package is not held to its listing: only `plugin.py` is compared with the
manifest's digest.

## What is not decided

- **Recommended, not decided:** that the plugin contract is frozen as version 1 now (as a draft
  until its consumers' fixtures pass) rather than after launch.
- **Recommended, not decided:** the five permission names that are new in version 1 (`surface`,
  `command`, `subscribe`, `tool`, `run`) and their order. An existing name never changes its place:
  permissions are only appended, so the signed bytes for an existing set never change.
- **Decided:** `runtime` is `python-sandboxed/1`, the name for Python run by a host in a process of its
  own outside the daemon; a plugin author writes the same `plugin.py` as before. In this build a plugin
  of either runtime runs in the daemon's own process and nothing is contained yet; the name
  python-sandboxed/1 is accepted so that manifests can be written for the host that follows.
  `python-inprocess/1` retires: every 1.0.x release accepts it and runs it the same way as
  `python-sandboxed/1`, with the finding `runtime_inprocess_retiring`, and from 1.1.0 on it is
  refused `runtime_retired`. Runtimes are only ever appended. Names held for a later release (two
  runtimes, three permissions, five record kinds) are listed under `$defs/reserved` in
  `manifest.v1.json`; each is refused with its own text, and reserved is not frozen.
- **Not decided:** what an install does with a revoked-key list that has expired or cannot be
  fetched. Until that is answered, the last accepted list stays in force, and the absence of a list
  never means that nothing is revoked.

## What may still change

- The five new permission names and their order (above).
- The pattern for a path in `files` refuses any segment that starts with a dot. That keeps out `.`
  and `..` without a look-ahead, and it also keeps out dot-files; PLUGIN-SIGN confirms.
- `install.json` has no version field, because today's reader refuses a field it does not know.
  The install commands may need more grant fields (a grant bound to the dependency lock or to the
  install scripts); those are additions.
