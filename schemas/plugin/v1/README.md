# Plugin records, version 1 (drafts)

The records a plugin package and its install share with the core. Each is a JSON Schema (Draft
2020-12) with fixtures, checked by the client in `conformance/`. Every schema here is tagged
`1.0-draft.N`; a draft may still change, and `CHANGES.md` lists each change.

| File | Record | What it is for | Who must send a fixture |
|---|---|---|---|
| `manifest.v1.json` | `csl-plugin-manifest/1` | The manifest a developer signs: the ten fields of the present manifest, plus `runtime`, `interface` and `files` (every file of the package with its SHA-256) | PLUGIN-SIGN, PLUGIN-HOST, PAGE |
| `install.v1.json` | `install.json` | This install's trusted keys and grants, exactly as the daemon reads the file today | PLUGIN-SIGN, PLUGIN-HOST, INSTALL |

Still to come in this folder: the plugin API's entry points as plain data (`api.v1`) and the signed
list of revoked developer keys (`revoked-keys.v1`).

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

## What may still change

- The five new permission names and their order (above).
- The pattern for a path in `files` refuses any segment that starts with a dot. That keeps out `.`
  and `..` without a look-ahead, and it also keeps out dot-files; PLUGIN-SIGN confirms.
- `install.json` has no version field, because today's reader refuses a field it does not know.
  The install commands may need more grant fields (a grant bound to the dependency lock or to the
  install scripts); those are additions.
