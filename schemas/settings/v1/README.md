# A person's own settings, version 1 (draft)

One owner-only file, `$XDG_CONFIG_HOME/recursion/settings.toml` (`$HOME/.config/recursion/settings.toml`
when the variable is unset), holds the settings a person chooses for their own Recursion. It is TOML, so a
person writes it by hand and may keep the reason for a choice in a comment beside it. `settings.v1.json`
is its shape once parsed (JSON Schema, Draft 2020-12), tagged `1.0-draft.1`.

| Setting | Type | Default | What it does |
|---|---|---|---|
| `plugin_host.gil_check` | boolean | `true` | Whether each plugin host makes its GIL check; on by default, and a person may turn it off. Read by no host yet. |
| `plugin_memory.plugins` | list of strings | `[]` | Plugins listed here may keep saved data between runs. Recursion keeps it for them in its own state folder; no other plugin can read it, and Recursion never hands it to a task it runs, though the plugin itself may put what it saved into a task it asks for. Read when Recursion starts. Empty as shipped. |
| `plugin_memory.max_bytes_per_plugin` | whole number or `"none"` | `67108864` | The most saved data one plugin may keep, keys and values together, in bytes; none for no limit. |
| `plugin_memory.max_item_bytes` | whole number or `"none"` | `262144` | The largest one item of a plugin's saved data may be, in bytes; held to what fits one message between Recursion and a plugin, and shown so; none means only that bound. |
| `plugin_memory.max_items_per_plugin` | whole number or `"none"` | `100000` | The most items of saved data one plugin may keep; none for no limit. |
| `plugin_memory.list_page_max` | whole number or `"none"` | `1000` | The most keys one listing of a plugin's saved data answers; none means as many as fit one message. |
| `plugin_memory.calls_per_minute` | whole number or `"none"` | `6000` | The most calls on its saved data one plugin may make in one minute; none for no limit. |
| `plugin_memory.sync` | text | `full` | How each write of saved data reaches the disk: full waits for the disk; normal is faster and may lose the last writes if the power fails. Any other text reads as full. |
| `plugin_memory.wal_max_bytes` | whole number or `"none"` | `16777216` | How large the write-ahead file of a plugin's saved data may grow, in bytes, before Recursion folds it into the store ahead of the next write; none for no limit. |
| `plugin_memory.record_each_write` | boolean | `true` | Whether every write and delete of saved data leaves a record row; false leaves one summary when the plugin's host ends. |
| `plugin_memory.record_key_text` | boolean | `false` | Whether a record row keeps the text of a saved-data key; false keeps only its SHA-256 and its length. |

## What holds for the file

- Every setting has a default; the file and every key in it may be absent.
- It is read only from that one path of the daemon's own environment: never from the current folder, a
  project, a run's workspace or environment, a plugin's folder, message or manifest, or a command line.
- It is used only if it is a regular file (not a link), owned by the daemon's user and not writable by
  group or others. Otherwise every default is in force, with the note `settings_unreadable`.
- Nothing in the product writes it, and no model or plugin writes it: a person keeps what a model proposes.
- A key the reader does not know is skipped and reported (`settings_unknown_key`); the others are read.
- A value of the wrong type or below a numeric setting's meaningful floor keeps the default
  (`settings_value_out_of_range`). A length or count, for example, has no meaning below zero.
- A numeric setting can opt in to the TOML string `"none"`, which means no limit. Status shows it as
  `no limit`. A numeric setting that does not opt in, a true-or-false setting and a list setting refuse that
  string with a note; a text setting keeps it as its text. Numeric values must be finite.
- A list setting takes a TOML array of strings, including `[]`. Every item must be a string. The reader
  keeps the first occurrence of each name in a tuple, and status shows the names in order.
- A numeric setting can give advice on how low or high to go. Status shows the advice beside the value
  and marks a number outside it, and no limit when the advice has an upper bound. That value stays in force.
- On a system without an owner check (Windows) no file is read (`settings_not_read_on_this_system`).
- `python -m recursion_core.settings` shows each setting, its value in force, its default, its source and
  any advice, and every note; it takes no argument and writes nothing. The daemon logs the same lines at start.
- The daemon reads the file at start in this build; a later reload may read it again.
- The list of settings is append-only: a row is added, never renamed or given another meaning.

## Fixtures

`fixtures/good/` holds a file read whole with no note; `fixtures/bad/` holds a file whose one value keeps
its default with a note. `core/recursion_core/daemon/tests/test_settings.py` reads both and checks that
this schema declares exactly the reader's table.
