# A person's own settings, version 1 (draft)

One owner-only file, `$XDG_CONFIG_HOME/recursion/settings.toml` (`$HOME/.config/recursion/settings.toml`
when the variable is unset), holds the settings a person chooses for their own Recursion. It is TOML, so a
person writes it by hand and may keep the reason for a choice in a comment beside it. `settings.v1.json`
is its shape once parsed (JSON Schema, Draft 2020-12), tagged `1.0-draft.1`.

| Setting | Type | Default | What it does |
|---|---|---|---|
| `plugin_host.gil_check` | boolean | `true` | Whether each plugin host makes its GIL check; on by default, and a person may turn it off. Read by no host yet. |
| `status.value_max_chars` | integer, or the string `"none"` | `80` | The most characters of one value that status, the start log and a note show; what is past it is cut and counted, a list shows the names that fit and how many more, and "none" shows every value whole. The least is 40, so the words that say what is cut or hidden always fit. |
| `status.hidden_names` | list of strings | `["key", "keys", "token", "tokens", "secret", "secrets", "password", "passwords", "passwd", "credential", "credentials"]` | The words that hide a setting's value: status, the start log and a note never show the value of a setting whose name holds one of them, whatever the value looks like (a name's words stand between dots, underscores and dashes). The words a settings file lists are added to these shipped words, which no settings file can take away; an empty list keeps the shipped words alone. To show one setting's value anyway, name that setting in status.shown_names. |
| `status.shown_names` | list of strings | `[]` | The settings whose value is shown although the name holds a word of status.hidden_names, each named whole as status shows it (such as db.api_token); empty as shipped. Each start and each status that uses this list says so in a note that names those settings. A text that looks like a key or password and the sign-in of an address stay hidden in such a value. |

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
- Every value those lines and the notes show passes one cap, `status.value_max_chars` (40 or more): a text
  past it is cut, with the count of characters not shown; a list shows the names that fit whole and how
  many more; `"none"` shows every value whole.
- Never shown, under any cap and under `"none"`, not in part and not by its length: the value of a setting
  whose name holds a word of `status.hidden_names` (the first net: by name, whatever the value looks like);
  the sign-in of a web address, what stands between the scheme and the host, also without a scheme and
  inside a longer text (`postgres://<sign-in hidden>@db/x`); and a text that looks like a key or a password.
  That look-alike rule is only a second net: it misses a short key, a key of one case or of digits only, a
  UUID, base64 cut by `+` or `/`, and a key split over a list's items, so such a value under a name not on
  the list is shown. Put a key only under a setting whose name says so.
- A settings file adds words to `status.hidden_names` and never takes the shipped words away; an empty
  list keeps them alone. A setting named whole in `status.shown_names` is shown although its name holds
  such a word, and every start and every status says so in the note `settings_shown_anyway`; the
  look-alike rule and the sign-in rule still hide what they find in its value.
- Every line is one line: a line break, a control character or a direction or format character in a value
  is shown escaped (`\n`, `\u202e`).
- The daemon reads the file at start in this build; a later reload may read it again.
- The list of settings is append-only: a row is added, never renamed or given another meaning.

## Fixtures

`fixtures/good/` holds a file read whole with no note; `fixtures/bad/` holds a file whose one value keeps
its default with a note. `core/recursion_core/daemon/tests/test_settings.py` reads both and checks that
this schema declares exactly the reader's table.
