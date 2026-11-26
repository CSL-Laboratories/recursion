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
| `status.digest_chars` | integer, 6 or more (64 advised at most) | `8` | How many hex characters of the SHA-256 digest of a key's text, with what the sign-in rule hides taken out, a key cut at the cap carries in a note, so two long keys that differ only past the cap read apart; a key that fits the cap carries none. The least is 6, since below it two keys read the same too often; past 64, the whole digest, nothing more is shown, so 64 is the advised most. A cut key carries only as many as fit the cap beside its first 8 characters, and never fewer than 6. |
| `status.label_by_rule` | true or false | `false` | Whether a text with no declared kind (a list item, a refused value, a key of the file) is also hidden whole when a word of status.label_words or status.hidden_names stands in it as a password label by rule: as a part of a name or the end of its last part, in any case, read again with percent-escapes, look-alike letters and format characters folded, with anything after it, and a known key prefix in any case. False as shipped keeps the earlier rule, which hides less ordinary text (such as 'a token budget of 5') and lets more ways of writing a password through; true hides those, and what it hides stays in your settings file as you wrote it. |
| `status.label_words` | list of strings | `["password", "passwd", "secret", "apikey", "bearer", "token", "pwd", "pass", "passphrase", "pw", "auth", "pin"]` | The words read as a password label when status.label_by_rule is true, beside the words of status.hidden_names; not read while it is false. The words a settings file lists are added to these shipped words, which no settings file can take away; an empty list keeps the shipped words alone. |

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
  A row declared to hold a secret shows nothing that depends on its value: no such mark, and a refused value
  of it is told only by the rule it breaks.
- On a system without an owner check (Windows) no file is read (`settings_not_read_on_this_system`).
- `python -m recursion_core.settings` shows each setting, its value in force, its default, its source and
  any advice, and every note, and, when a line hides a value, closing lines that count the lines
  hiding one and name where the person reads it: the settings file, by its path, for a value the
  file holds (and that it is not used, where it is not), and the product's settings module for a
  shipped default. It takes no argument and writes nothing. The daemon logs the same lines at
  start, never the closing lines.
- Every value those lines and the notes show passes one cap, `status.value_max_chars` (40 or more): a text
  past it is cut, with the count of characters not shown; a list shows the names that fit whole and how
  many more; `"none"` shows every value whole. A key of the file is capped the same way: a key that is cut
  carries the first `status.digest_chars` hex characters (8 as shipped) of the SHA-256 of its text
  after the sign-in rule's hiding (below), said to be a digest, so two keys never read the same and
  nothing that rule hides enters the hash; it carries only as many as fit the cap beside the key's first 8
  characters, and never fewer than 6. A key that fits is shown whole.
- Never shown, under any cap and under `"none"`, not in part and not by its length: the value and the
  default of a setting the core declares to hold a secret, a sign-in or an address, which show only
  `<set; hidden by its declaration>` or `<not set>`, whatever `status.shown_names` says (no shipped setting
  declares one yet; two notes still count lines: the note for a file that is not UTF-8 and the note for a
  file that is not TOML name only the line of the first place the file cannot be read (or that the TOML
  reader met the end of the document), never its column, and nothing else of that line; the line counts the
  lines before that place, so for a declared value written over several lines it tells how many of its lines
  came before it, and the note's words tell which kind of fault it met); the value of a setting
  whose name holds a word of `status.hidden_names` (the first net: by name, whatever the value looks like);
  a text that looks like a key or a password; and a sign-in, by one rule on each run of text (the sign-in
  rule). A run is what stands between two white-space characters as Python counts them (a space, a tab, a
  line break, a no-break space, U+2028, U+001C to U+001F and the others); a zero-width character such as
  U+200B does not end a run. In a run that holds an @, everything before its last @ is hidden, except a
  scheme and its mark at the very start of the run (ASCII letters, digits, `+`, `.` and `-`, then `://`),
  which stay: `postgres://<sign-in hidden>@db/x`, `<sign-in hidden>@db/x`. Nothing else before that @ is
  shown: not a user name or a part of it, not a label, not the host or the path of an earlier address (two
  joined addresses show as `a://<sign-in hidden>@h2/`, `https://h/u@mail` as
  `https://<sign-in hidden>@mail` and `url=a://u:p@h` as `<sign-in hidden>@h`); what follows the last @ is
  shown. Everything before the last at-sign of a run is hidden; of an email address only the part after
  the at-sign is shown. The sign-in rule misses a sign-in that holds white space (the run ends there, and
  what stands before it is shown), an address written wholly percent-escaped, and a secret written as the
  scheme at the very start of a run. The look-alike rule
  is only a second net: it misses a short key, a key of one case or of digits only, a UUID, base64 cut by
  `+` or `/`, and a key split over a list's items, so such a value under a name not on the list is shown.
  A value or a key whose own text, as the person wrote it, carries a password label or a known key
  prefix is never shown, not in part, wherever an @ stands in it: `password=ab@cdefgh` is hidden whole.
  With `status.label_by_rule` set to true (false as shipped, which keeps only the rule just said), a
  label is also found by a rule, not a list of spellings: a name (words joined by `_`, `-` or `.`) in which a
  word of `status.label_words` (as shipped: password, passwd, secret, apikey, bearer, token,
  pwd, pass, passphrase, pw, auth, pin) or of `status.hidden_names` is a part, or ends the last part (`db_password`, `MyPassword`,
  `apitoken`), in any case, with anything after it; read on the text as written and again with
  percent-escapes decoded, Unicode compatibility forms and case folded, and combining marks and format
  characters such as U+200B dropped; a word in letters of more than one script is a label by that alone; a
  key prefix is read in any case; no length is asked after a label. It still misses a label wholly in
  another script, a look-alike letter Unicode does not fold, and a prefix split by a plain space, and it
  hides some ordinary text (`bypass=on`, `token@example.org`), which stays in the settings
  file as the person wrote it.
  Put a key only under a setting whose name says so.
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
its default with a note (`b01`) and a file the reader refuses whole (`b02`, `version = 1.0`). JSON Schema
cannot tell 1.0 from 1, so this schema accepts version 1.0; the reader takes only the whole number 1 and
refuses that file with `settings_unreadable`, keeping every default.
`core/recursion_core/daemon/tests/test_settings.py` reads them all and checks that this schema declares
exactly the reader's table.
