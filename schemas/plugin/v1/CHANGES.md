# Changes to the plugin records, version 1

Each schema carries its own version in `x-version`. While a schema is `1.0-draft.N`, any change bumps
`N` and is listed here with what it breaks. A schema becomes `1.0` when every consumer lane named for
it in `README.md` has a passing fixture in `fixtures/`.

## `wall.v1.json` is `1.0-draft.6` (Nov 24, 2026)

A proven record now needs truncation refused by Landlock from Landlock ABI 3 on, as every other kind of write
already is. The filter holds truncation only below ABI 3, where Landlock cannot refuse it, so a record that
credits the filter with a truncation at ABI 3 or above shows the filter and not the wall. What it breaks: a
draft 5 record that says proven at ABI 3 or above with `truncate` as `refused_by_filter` does not validate at
draft 6 (the host's own probe never gives one). Three bad fixtures are added: `b12` (truncation credited to
the filter at ABI 3), `b13` (every kind of write credited to the filter at ABI 6) and `b14` (truncation
refused elsewhere below ABI 3, which proves the rule for truncation below ABI 3 now that `b06`, at ABI 3, is
refused by the new rule too).

## `wall.v1.json` is `1.0-draft.5` (Nov 24, 2026)

An additive step: every field and value of draft 4 keeps its meaning, and a draft 4 record that is not proven
still validates (fixture `h15`). A record gains `threads` (the threads of the host's process when the probe
began) and `descriptors_open_for_write` (each descriptor held open for writing on a regular file or a block
device, by number and kind of file, or null when not every descriptor could be seen); `writes` gains
`make_socket_node` (a socket file made by mknod, now tried); `writes_not_tried` gains `rename_within_folder`,
`change_owner`, `change_times`, `change_extended_attributes`, `write_through_open_descriptor`,
`writable_memory_map` and `write_beneath_other_folder`, each with the reason it is not tried, and no longer
requires `make_socket_node`. A proven record now also needs one thread, no descriptor open for writing, the
socket kind refused by Landlock, every new kind named as not tried and the socket kind not named so, every
kind but truncate refused by Landlock (the filter holds truncation only), and both Landlock scopes from ABI 6
on. What it breaks: a draft 4 record that says proven does not validate at draft 5 (it never counted its
threads, looked for an open descriptor or tried a socket file). Every wall fixture is brought up to the
step; 4 good fixtures (`h12` to `h15`) and 20 bad ones are added, and the bad fixture that proved
`writes_not_tried` required `make_socket_node` is removed with that rule.

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

## `host.v1.json` is `1.0-draft.4` (2026-11-23)

The host's own `hello` may carry `wall`, the `wall.v1` record of the wall the host applied to itself
and probed from inside before it read any message. A host whose record is absent or not proven answers
every load with `plugin_wall_unproven` (a new code, placed before `load_too_large`), naming what is
missing in the detail, and runs no plugin code. Only the host sends `wall`; a daemon `hello` that carries
it is refused `message_invalid`. What it breaks: a reader that pinned the host's `hello` to the channel
version alone. Existing hello messages still validate.

## `host.v1.json` is `1.0-draft.3` (2026-11-21)

The daemon's first `hello` may carry `gil_check` (`on` or `off`). The host defaults to `on`
and its own `hello` carries only the channel version. With checking on, a host that reads its GIL
as enabled after a load or call refuses that work and every later load or call with
`host_gil_enabled`. A host that cannot start with a clean environment sends one `host_start_refused`
refusal (detail `restart_failed` or `environment_not_clean`) after its `hello` and ends. Existing hello
messages still validate.

Plugin code shares the host's process, so it can read and write the channel's two descriptors itself:
every frame on the channel is the plugin's voice, and the daemon must check each request against that
plugin's grant and take no result on trust.

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
