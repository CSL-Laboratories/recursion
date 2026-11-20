# Measured `claude --help`: is `--bare` the `-p` default? (CSL-726)

Measured data only. Nothing here changes `bare_default.py`, `claude_code.py` or the route's
refusal. It is input for the #323 launch-check floor decision (O50 / CX-956); Lucas picks the
policy.

Measured 2026-11-08 23:23 PST on linux-x64. Each build ran `--version` and `--help` only (offline,
no login, no model call) as:

```
env -i PATH=/usr/bin:/bin HOME="$HOME" LANG=C.UTF-8 COLUMNS=80 <claude> --help  # stdout+stderr
```

`help_sha256` is SHA-256 of that combined output (LF line endings, as committed; a CRLF
checkout changes the hash). `classifier_verdict_readonly` is
`bare_default.classify_help(<that file>)` at #336 head edc7653b8ed66f73a17f733503dc2bef8e81e6bd,
run read-only. npm `latest` for `@anthropic-ai/claude-code` was 2.1.319 at measurement time
(`npm view @anthropic-ai/claude-code version`); no newer build exists. npm publishes no 2.1.313.

| version | help exit | help_sha256 | `--bare` wording summary | print-default sentence | classifier_verdict_readonly | measured_at PST |
|---------|-----------|-------------|--------------------------|------------------------|-----------------------------|-----------------|
| 2.1.312 | 0 | `fc9af56f65f8a84a9e7e98cd538ccac13f57d1cc0e22099666508198ba88d64d` | "Minimal mode: skip hooks …, LSP, plugin sync, attribution, auto-memory, background prefetches, keychain reads, and CLAUDE.md auto-discovery. Sets CLAUDE_CODE_SIMPLE=1. Anthropic auth is strictly ANTHROPIC_API_KEY or apiKeyHelper via --settings (OAuth and keychain are never read). …" No default wording. | none | unknown | 2026-11-08 23:23 |
| 2.1.314 | 0 | `5ca3a9dac2026e56c221fdd34380c7d7d45ebbb51c1d01b7c17c75cb076a0be8` | Same `--bare` block as 2.1.312, byte for byte. | none | unknown | 2026-11-08 23:23 |
| 2.1.315 | 0 | `5ca3a9dac2026e56c221fdd34380c7d7d45ebbb51c1d01b7c17c75cb076a0be8` | Same as 2.1.314 (whole help identical). | none | unknown | 2026-11-08 23:23 |
| 2.1.316 | 0 | `5ca3a9dac2026e56c221fdd34380c7d7d45ebbb51c1d01b7c17c75cb076a0be8` | Same as 2.1.314 (whole help identical). | none | unknown | 2026-11-08 23:23 |
| 2.1.317 | 0 | `5ca3a9dac2026e56c221fdd34380c7d7d45ebbb51c1d01b7c17c75cb076a0be8` | Same as 2.1.314 (whole help identical). | none | unknown | 2026-11-08 23:23 |
| 2.1.318 | 0 | `5ca3a9dac2026e56c221fdd34380c7d7d45ebbb51c1d01b7c17c75cb076a0be8` | Same as 2.1.314 (whole help identical). | none | unknown | 2026-11-08 23:23 |
| 2.1.319 | 0 | `5ca3a9dac2026e56c221fdd34380c7d7d45ebbb51c1d01b7c17c75cb076a0be8` | Same as 2.1.314 (whole help identical). | none | unknown | 2026-11-08 23:23 |
| 2.1.320 | n/a | n/a | Not published. `npm view @anthropic-ai/claude-code@2.1.320` is 404 and the native release bucket has no `2.1.320/manifest.json` (404), checked 2026-11-09 12:17 PST. Nothing to measure. | n/a | n/a | 2026-11-09 12:17 |
| 2.1.321 | 0 | `5ca3a9dac2026e56c221fdd34380c7d7d45ebbb51c1d01b7c17c75cb076a0be8` | Same as 2.1.314 (whole help identical). npm `latest` and `next` as of 2026-11-09 (published 2026-11-09 11:02 PST). | none | unknown | 2026-11-09 12:17 |
| 2.1.311 | 0 | `fc9af56f65f8a84a9e7e98cd538ccac13f57d1cc0e22099666508198ba88d64d` | Same as 2.1.312 (whole help identical). npm `stable` as of 2026-11-09, below the 2.1.314 floor. | none | unknown | 2026-11-09 12:17 |

Notes:

- The only difference between 2.1.312 and 2.1.314 help is the `auto-mode` subcommand line
  ("Inspect or reset auto mode classifier configuration" became "Inspect or reset auto mode
  configuration"). 2.1.314 through 2.1.319 help is identical.
- No build lists `--no-bare`. No `-p, --print` block mentions bare. The `-p, --print` block
  begins "Print response and exit (useful for pipes)." The usage line says "starts an
  interactive session by default, use -p/--print for non-interactive output".
- The only other help text that names `--bare` with "default" is in `--system-prompt-snapshot`:
  "on (the default, except under --bare outside cloud and background sessions)". It is about
  the snapshot default, not about `--bare` being the `-p` default.
- So help text alone neither shows bare is the `-p` default nor says it is not; the classifier
  returns `unknown` for every build, and the route refuses before launch as it does today.

Fixtures (full help, stdout+stderr): `claude-2.1.311-help.txt`, `claude-2.1.312-help.txt`,
`claude-2.1.314-help.txt`, `claude-2.1.319-help.txt`, `claude-2.1.321-help.txt`. 2.1.315 to 2.1.318 have the 2.1.314 hash, so they have no separate
file. `claude-2.1.312-help-excerpt.txt` is the earlier excerpt, unchanged.

Agent (Grok Bot Liaison, for Lucas): second measurement, 2026-11-09 12:17 PST, same command and
platform, npm installs into a scratch prefix (install scripts not run; `--version` printed the
expected build). npm dist-tags then: `latest` 2.1.321, `next` 2.1.321, `stable` 2.1.311. 2.1.319
was re-measured and still matches its committed fixture byte for byte. 2.1.321 help is identical
to 2.1.314/2.1.319 and 2.1.311 help is identical to 2.1.312; both are committed as their own files
so each measured build has a fixture. 2.1.320 was never published. `classifier_verdict_readonly`
for the new rows is `classify_help` at #336 head 5138c4181787b743348e57e3154beb1d28010e4b. The
refusal logic is unchanged; the subscription route still refuses at every measured build.

## Admission table (one entry)

Nov 9 owner call. The measurement rows above stay as evidence. The subscription
route's allow table is this one hash, not one row per version. Classifier
`unknown` is not an allow. A build runs as beta only when its `claude --help`
text hashes to this entry and the build is at or above the 2.1.314 floor. Help
that differs names the build and needs re-measuring. Below the floor refuses.

| help_sha256 | label | floor |
|-------------|-------|-------|
| `5ca3a9dac2026e56c221fdd34380c7d7d45ebbb51c1d01b7c17c75cb076a0be8` | beta | 2.1.314 |
