# Changes to the plugin records, version 1

Each schema carries its own version in `x-version`. While a schema is `1.0-draft.N`, any change bumps
`N` and is listed here with what it breaks. A schema becomes `1.0` when every consumer lane named for
it in `README.md` has a passing fixture in `fixtures/`.

## `install.v1.json` is `1.0-draft.4` (Nov 17, 2026)

The install file may carry `registry_labels`: a map from the SHA-256 of one installed manifest's bytes
to `verified` or `non_verified`. Draft 3 had no place for that label, and the reader refused any
top-level field it did not know, so a label the installer had seen could not be stored and every plugin
was shown unlisted. What it breaks: a label other than those two, and a key that is not 64 lower-case
hex digits. An omitted map and an empty map are still no label. The label decides nothing: it is not a
permission and not a grant. One good fixture (`h05`) and one bad fixture (`b10`) hold the new list of
values. A label whose key is not the bytes of the manifest now installed is the reader's rule, and a
schema cannot see that file (`README.md`). The developer-key bump stays `1.0-draft.3`; this is the next bump.

## `install.v1.json` is `1.0-draft.3` (Nov 17, 2026)

A grant may name `developer_key_id` as null or as a key id (`ed25519:` and 32 lower-case hex digits). Draft 2
allowed the member and the reader refused it as an unknown grant field, so a file of the schema was not read.
The reader now takes that shape and refuses any other. What it breaks: a `developer_key_id` that is not null
and not a key id. A file that omits the member is unchanged. No rule here ties `developer_key_id` to
`signer_key_id`. The reader checks the shape and does not store the id on the grant.

## `revoked-keys.v1.json` is `1.0-draft.2` (Nov 15, 2026)

Its `timestamp` (a list's `issued_at` and `expires_at`, and each key's `revoked_at`) is now the control
records' timestamp: a real UTC calendar instant, marked by the keyword `x-instant`, which the conformance
client checks, with seconds from 00 to 59 in the pattern and in the check. Draft 1 had neither: a list
that expired on the 31st of February fitted (a reader of the pull request showed it), and so did a second
of 60. No other definition in this folder spells a time: `manifest.v1.json`, `install.v1.json` and
`api.v1.json` hold no time, no `format` keyword and no reference into another file. What it breaks: a
list that names a day that does not exist, or a second of 60. Three bad fixtures (`b07` to `b09`) and two
tests in `conformance/tests.py` hold it; one of them requires the keyword and the seconds of every
timestamp in every folder of schemas.

## `install.v1.json` is `1.0-draft.2` (Nov 13, 2026)

A grant lists only the seven permissions today's reader takes. Draft 1 let a grant list the five names
that are new in the version 1 manifest (`surface`, `command`, `subscribe`, `tool`, `run`), and today's
reader refuses such a file (`daemon/plugins.py`, `read_install`), so the schema's own claim, that a
file written to it is read by today's daemon, was false. What it breaks: an install file that grants
one of the five. Granting them needs the reader to change first; this schema will say so when it does.

## Fixtures that prove every rule (Nov 13, 2026)

No schema changed. Each `additionalProperties: false`, each name in a `required` list, each `enum` and
each `const` is now taken away, one at a time, and a bad fixture must notice (see
`conformance/README.md`). In this folder: 17 closed objects (17 proved before), 54 required fields (6
before), 6 lists of allowed values (3 before) and 8 fixed values (4 before); all 85 are proved now, by
68 generated fixtures (`u-*`, `m-*`, `v-*`, `f-*`: a good fixture with one change) and one more good
fixture written by hand (a run in a worktree).

## 1.0-draft.1 (Nov 13, 2026)

First drafts. Nothing is frozen.

| Schema | Version | Notes |
|---|---|---|
| `manifest.v1.json` | 1.0-draft.1 | The ten present fields, plus `runtime`, `interface`, `files`; five provisional permission names appended |
| `install.v1.json` | 1.0-draft.1 | Today's `install.json`, with nothing added |
| `api.v1.json` | 1.0-draft.1 | A skeleton of the entry points as data, and five rules that hold whatever the shapes become |
| `revoked-keys.v1.json` | 1.0-draft.1 | The minimum: a rising list version, issue and expiry times, the revoked keys, the list key's signature |

### Provisional choices in this draft

Built as recommended and **not decided**:

| Choice | Built as | Alternative | Whose |
|---|---|---|---|
| Freeze the plugin contract as version 1 | Now, as a draft until consumers' fixtures pass | After launch | The owner's |
| New permission names and their order | `surface`, `command`, `subscribe`, `tool`, `run`, appended after `order` | Other names, or none | The owner's |
