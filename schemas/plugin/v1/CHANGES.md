# Changes to the plugin records, version 1

Each schema carries its own version in `x-version`. While a schema is `1.0-draft.N`, any change bumps
`N` and is listed here with what it breaks. A schema becomes `1.0` when every consumer lane named for
it in `README.md` has a passing fixture in `fixtures/`.

## `wall.v1.json` is `1.0-draft.4` (Nov 23, 2026)

A record gains `writes` and `writes_not_tried`, both required. `writes` holds, for each of eleven kinds of
change (open a file for writing, cut it to its own length, make a file, a folder, a named pipe, a symbolic
link, a hard link, a character and a block device, remove a file and a folder), the outcome of trying it
behind the wall on a file and in a folder made for the purpose: `refused_by_landlock`, `refused_by_filter`,
`refused_elsewhere`, `let_through`, `error` or `not_tried`. `proven` needs every kind refused by the part of
the wall that holds it (the filter holds only truncation, below Landlock ABI 3); draft 3 called a wall
proven that left writes open, because it never tried one. `writes_not_tried` names the three kinds the
probe cannot try harmlessly, each with the check that shows it refused instead. What it breaks: a record
without the two fields, so a host built on draft 3 reads as not proven and loads nothing until it hands the
probe a file and a folder. Every wall fixture carries both fields; 4 good fixtures (`h08` to `h11`) and
21 bad ones hold them.

## `wall.v1.json` is `1.0-draft.3` (Nov 21, 2026)

A record gains `landlock_scopes`, required: the Landlock scopes the wall put in force (`abstract_unix_socket`,
`signal`), asked from Landlock ABI 6 on and empty below. `kernel_surface` also tries msgctl, mq_getsetattr,
a signal 0 to the host itself and a lease on a pipe, so a filter that lets any of them through reads not
proven. The description now says what the record is: a check the host's own code makes on itself before any
plugin code runs, which means something only when it is made before the load and read only from frames sent
before it. Draft 2 added `signal_other_ways`, `metadata_change` and `kernel_surface`. What it breaks: a
record without `landlock_scopes`. Every wall fixture carries the field; one good fixture (`h07`) and three
bad ones (`b05`, `m-wall.v1.landlock-scopes`, `v-wall.v1.landlock-scopes`) hold it.

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
