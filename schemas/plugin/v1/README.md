# Plugin records, version 1 (drafts)

The records a plugin package and its install share with the core. Each is a JSON Schema (Draft
2020-12) with fixtures, checked by the client in `conformance/`. Every schema here is tagged
`1.0-draft.N`; a draft may still change, and `CHANGES.md` lists each change.

| File | Record | What it is for | Who must send a fixture |
|---|---|---|---|
| `manifest.v1.json` | `csl-plugin-manifest/1` | The manifest a developer signs: the ten fields of the present manifest, plus `runtime`, `interface` and `files` (every file of the package with its SHA-256) | PLUGIN-SIGN, PLUGIN-HOST, PAGE |
| `install.v1.json` | `install.json` | This install's trusted keys and grants, exactly as the daemon reads the file today | PLUGIN-SIGN, PLUGIN-HOST, INSTALL |
| `api.v1.json` | `csl-plugin-api/1` | The plugin API's entry points as plain data. A skeleton: the shapes are for the lanes that host plugins and draw the page to supply | PLUGIN-HOST, PAGE |
| `revoked-keys.v1.json` | `csl-revoked-keys/1` | The signed list of revoked developer keys. A draft: revocation's details are open | PLUGIN-SIGN |

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

## What is not decided

- **Recommended, not decided:** that the plugin contract is frozen as version 1 now (as a draft
  until its consumers' fixtures pass) rather than after launch.
- **Recommended, not decided:** the five permission names that are new in version 1 (`surface`,
  `command`, `subscribe`, `tool`, `run`) and their order. An existing name never changes its place:
  permissions are only appended, so the signed bytes for an existing set never change.
- `runtime` has one value, `python-inprocess/1`: ordinary Python in the daemon's own process and
  user. That is not a containment boundary.
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
