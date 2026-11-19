# Changes to the plugin records, version 1

Each schema carries its own version in `x-version`. While a schema is `1.0-draft.N`, any change bumps
`N` and is listed here with what it breaks. A schema becomes `1.0` when every consumer lane named for
it in `README.md` has a passing fixture in `fixtures/`.

## `host.v1.json` is `1.0-draft.2` (2026-11-18)

`call` and `result` travel both ways: the daemon calls a plugin's function by name, and a plugin's host
calls a broker handle by name; when built, every reserved call name is sent by a host to the daemon, and a
reserved name arriving at the daemon is refused `call_reserved` before any other check of it. A result for
an id the receiver never called is refused. `net.request` and `net.connect` join the reserved call names.
A `load` frame (daemon to host only) may be as large as the daemon's largest package needs, computed from
`MAX_MANIFEST_BYTES` and `MAX_ARTIFACT_BYTES` (now in `plugin_security/limits.py`, which
`daemon/plugins.py` reads); every other frame, and every frame a
host sends, stays at most 1 MiB; a package that still does not fit is refused `load_too_large` (a new code,
appended) on the daemon's side. One channel per loaded plugin: every load names the plugin the channel was
opened for. The four patterns without it gain the line-break guard. What it breaks: nothing merged; inside
this draft, `call` was daemon-only and `result` host-only, and the frame limit was 1 MiB for every frame.
Six more bad fixtures prove the load's file paths and plugin id, the id range, the detail length and the
line-break guard.

## `host.v1.json` is new, `1.0-draft.1` (2026-11-18)

The channel between the daemon and a plugin host: `hello`, `load`, `call`, `core`, `result`, `refusal`
and `bye`, framed as `control_transport/framing.py` frames. A `call` carries a name from one
append-only list (`registry`, `operator`, `surfaces`, `surface`); a name the receiver does not know is
refused `call_unknown`, and a later surface kind is a new name, never a schema break. A message type the
receiver does not know is answered with a typed refusal and the channel stays up. The message names
`event`, `guard` and `surface.update` and the call names `run.request` and `computer.open`, `.act`,
`.observe` and `.close` are reserved: named, answered 'reserved; not available in this release', never
built here. The run-origin values `trigger`, `agent` and `packet` are in the reserved list with no field
yet: a message that carries a field for one is refused as any unknown field is (`message_invalid`), not
answered 'reserved'. No message carries a caller: the
daemon sets it from the channel. What it breaks: nothing; no schema before it named these messages.
Forty-nine fixtures prove its rules; `plugin_security/host_channel.py` is its code side and
`plugin_security/tests/test_host_channel.py` holds the two equal.

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
