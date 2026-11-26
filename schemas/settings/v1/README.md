# A person's own settings, version 1 (draft)

One owner-only file, `$XDG_CONFIG_HOME/recursion/settings.toml` (`$HOME/.config/recursion/settings.toml`
when the variable is unset), holds the settings a person chooses for their own Recursion. It is TOML, so a
person writes it by hand and may keep the reason for a choice in a comment beside it. `settings.v1.json`
is its shape once parsed (JSON Schema, Draft 2020-12), tagged `1.0-draft.1`.

| Setting | Type | Default | What it does |
|---|---|---|---|
| `plugin_host.gil_check` | boolean | `true` | Whether each plugin host makes its GIL check; on by default, and a person may turn it off. Read by no host yet. |

## What holds for the file

- Every setting has a default; the file and every key in it may be absent.
- It is read only from that one path of the daemon's own environment: never from the current folder, a
  project, a run's workspace or environment, a plugin's folder, message or manifest, or a command line.
- It is used only if it is a regular file (not a link), owned by the daemon's user and not writable by
  group or others. Otherwise every default is in force, with the note `settings_unreadable`.
- Nothing in the product writes it, and no model or plugin writes it: a person keeps what a model proposes.
- A key the reader does not know is skipped and reported (`settings_unknown_key`); the others are read.
- A value of the wrong type or outside its range keeps the default (`settings_value_out_of_range`); a
  value is never clamped.
- On a system without an owner check (Windows) no file is read (`settings_not_read_on_this_system`).
- `python -m recursion_core.settings` shows each setting, its value in force, its default and its source,
  and every note; it takes no argument and writes nothing. The daemon logs the same lines at start.
- The daemon reads the file at start in this build; a later reload may read it again.
- The list of settings is append-only: a row is added, never renamed or given another meaning.

## Fixtures

`fixtures/good/` holds a file read whole with no note; `fixtures/bad/` holds a file whose one value keeps
its default with a note (`b01`) and a file the reader refuses whole (`b02`, `version = 1.0`). JSON Schema
cannot tell 1.0 from 1, so this schema accepts version 1.0; the reader takes only the whole number 1 and
refuses that file with `settings_unreadable`, keeping every default.
`core/recursion_core/daemon/tests/test_settings.py` reads them all and checks that this schema declares
exactly the reader's table.
