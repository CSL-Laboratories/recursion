# Changes to the plugin records, version 1

Each schema carries its own version in `x-version`. While a schema is `1.0-draft.N`, any change bumps
`N` and is listed here with what it breaks. A schema becomes `1.0` when every consumer lane named for
it in `README.md` has a passing fixture in `fixtures/`.

## `host.v1.json` is `1.0-draft.3` (2026-11-24)

The root of the file now applies its seven message forms (`oneOf` over `$defs`): before, an outside
validator pointed at the file accepted any value at all. The texts say what is true at this head: the
daemon's end sends no call, and the daemon's own call of a plugin's function through the channel is later
work. The channel's order is written down: a `bye` ends the channel at any time, also before the `hello`;
after a `bye` every later message is refused `message_invalid`; a second `hello` is refused
`message_invalid`. An id is a whole number written without a fraction; the schema cannot decide that, and
says so. A refusal of ours uses only a listed code, while an arriving refusal with an unlisted code is
still read as a refusal. `call_failed` also answers a function that tried to end the host process and a
value that cannot be written as JSON; `load_too_large` also answers a manifest or a file over its own
size. What it breaks: a value that matches none of the seven forms no longer validates at the root; no
message definition changed.

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
yet: a field for a run origin is refused as any unknown field is (`message_invalid`), not answered
'reserved', only on a defined message type that reaches its right receiver; on a reserved message type
(`event`, `guard`, `surface.update`), the reserved answer (`message_reserved`) is given first, and on a
defined message type from the wrong side, `message_wrong_side` is given, both before any field is read.
No message carries a caller: the
daemon sets it from the channel. What it breaks: nothing; no schema before it named these messages.
Forty-nine fixtures prove its rules; `plugin_security/host_channel.py` is its code side and
`plugin_security/tests/test_host_channel.py` holds the two equal.

## Loader admission

A plugin that requests permissions with an unbound manual grant is refused `grant_unbound`
before its code runs, whether its manifest is signed or not. An `order_after_revoked_key` that is
not a developer key ID binds nothing and is refused where a grant is read. A signed version 1 load
is refused `package_file_missing` when a file listed in the manifest is absent. The refusal names
the first missing path in manifest path order. When the package folder itself is missing, is a
symbolic link or is not a folder, the refusal says that instead.

On a signed version 1 load from a package folder, every listed file is then read before any plugin
code runs. A listed file, or a folder on its path, that is not the daemon user's alone is refused
`package_file_untrusted`; a listed file whose SHA-256 is not the manifest's is refused
`package_file_digest_mismatch`; any other name in the package folder or in a folder on a listed
path, except `manifest.json` and `manifest.sig` at the top, is refused `package_file_unlisted`. Each
refusal names the path. A missing file is still named first. What it breaks: a signed version 1
package with a changed or an extra file (a `__pycache__` folder included), or with a file a group or
other users can write, loaded before as signed and is refused now. No schema changed, and an
unsigned package is held to nothing new.

Such a load is also refused `package_compiled_file_prefix` when, at the moment of the check, the
interpreter keeps its compiled files outside the package (`PYTHONPYCACHEPREFIX`, `-X pycache_prefix`
or a value set in the process, read from `sys.pycache_prefix` once per load, after the package
checks), because Python would import a compiled file from that prefix in place of a checked listed
file. The refusal names the prefix and says to start the daemon without it. What it breaks: a
daemon started with such a prefix loads no signed package from a folder until it is started without
it.

What the checks do not cover (`README.md`): a listed file changed after the check, which only a
writer with the daemon user's own rights can do; a module of the same name that Python finds before
the listed file (one the daemon's process already holds, one built into or frozen in the
interpreter, or one earlier on the import path than the package folder); and code already running
in the daemon's process (the daemon's own start, a site file of the interpreter, or a plugin loaded
earlier, signed or not and with no grant), which can install a finder or import hook, or set a
compiled-file prefix after the loader has looked at it, so that Python hands the plugin other code
for a listed module's name. That code runs under the plugin's signed name and grants, and the
plugin's record still says every listed file was checked.

## `manifest.v1.json` is `1.0-draft.2` (2026-11-18)

`runtime` takes `python-sandboxed/1` (the name for a plugin run by a host outside the daemon; in this
build no plugin is contained yet, and a `python-sandboxed/1` load gives the warning `runtime_not_yet_contained`)
beside `python-inprocess/1`, which retires: it is read as `python-sandboxed/1` in every 1.0.x release,
with the finding `runtime_inprocess_retiring`. A new `$defs/reserved` holds the names kept for a later
release, each with a sentence on what it is for: the runtimes `wasm-component/1` and `data/1`, the
permissions `schedule`, `memory` and `computer`, and the record kinds `trigger.v1`, `agent.v1`,
`memory.lesson.v1`, `work.packet.v1` and `node.v1`. The parser refuses a reserved runtime
`runtime_reserved` and a reserved permission `permission_reserved`, each with its own text; any other
runtime is refused `manifest_runtime` with a text that names what this release runs. What it breaks:
nothing that loads today. Every manifest draft 1 accepted is still accepted; a manifest that names a
reserved name was refused before and is refused now, under its own code. One good fixture (`h04`) and
three bad ones (`b12`, `b13`, `b14`) hold it.

## `install.v1.json` is `1.0-draft.4` (2026-11-17)

The install file may carry `registry_labels`: a map from the SHA-256 of one installed manifest's bytes
to `verified` or `non_verified`. Draft 3 had no place for that label, and the reader refused any
top-level field it did not know, so a label the installer had seen could not be stored and every plugin
was shown unlisted. What it breaks: a label other than those two, and a key that is not 64 lower-case
hex digits. An omitted map and an empty map are still no label. The label decides nothing: it is not a
permission and not a grant. One good fixture (`h05`) and one bad fixture (`b10`) hold the new list of
values. A label whose key is not the bytes of the manifest now installed is the reader's rule, and a
schema cannot see that file (`README.md`). The developer-key bump stays `1.0-draft.3`; this is the next bump.

## `install.v1.json` is `1.0-draft.3` (2026-11-17)

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
