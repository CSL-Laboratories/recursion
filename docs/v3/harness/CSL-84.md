# CSL-84 — stream fixtures and the Windows doctor shim

Date: 2026-10-15. Parent issue: CSL-84. Parent goal: CSL-27.

No provider CLI was started. Parallel Search MCP was not connected in this
session, and `parallel-cli` was not installed. The pages named in each fixture
header were fetched directly.

## Fixtures

Directory: `scripts/tests/v3/harness/fixtures/`.

| File | Evidence | CLI version line |
|---|---|---|
| `grok-streaming-json.txt` | source-documented | unpinned; usage events began in Grok Build 0.2.116 |
| `grok-review.txt` | synthetic | unpinned |
| `codex-exec-jsonl.txt` | source-documented | unpinned on the OpenAI page |
| `codex-cumulative.txt` | synthetic | unpinned; 6.9M figure is the example in openai/codex#17539 |
| `claude-auth-status.txt` | source-documented | 2.1.300, the CSL-81 changelog pin (2026-10-14). The paste itself says Claude Code 2.x |
| `claude-auth-status-exit-1.txt` | synthetic | 2.1.300, the same CSL-81 pin. That binary did not produce the file |
| `claude-auth-status-redaction.txt` | synthetic | 2.1.300, the same CSL-81 pin. Stdout carries the test sentinel `SECRET-TOKEN-VALUE`. That binary did not produce the file |
| `claude-stream-json.txt` | synthetic | 2.1.300, the same CSL-81 pin. `result` differs from `message.content`. That binary did not produce the file |

`# evidence: synthetic` is the synthetic mark. Every file has `# cli-version:`.
A published line that is not valid JSON is omitted, and the header says so.

## Parsers

`scripts/tests/v3/harness/harness.test.py` loads these files. The verdict and
usage parsers read the stream fixtures. Claude verdict text is
`message.content`, and the stream fixture's `result` string is different, so
the test fails if the parser reads `result`. The doctor parser reads the
Claude auth-status fixtures, including exit 1 with `loggedIn: true`. The
redaction fixture's stdout contains `SECRET-TOKEN-VALUE`; the rendered doctor
report does not.

## Windows

`test_doctor_windows_path_shim_does_not_start_a_provider_cli` runs when
`sys.platform == "win32"`. It resolves a `claude.cmd` shim and stubs the
probe. It does not start Claude, Codex, or Grok.

The existing `windows` job in `.github/workflows/ci.yml` already runs
`python -X utf8 -B tools/ci_gate.py --force` on a pull request labeled
`ci:full`. That gate includes `scripts/tests/v3/harness/harness.test.py`.
No workflow file was added or edited. The required `linux` job stays
provider-free.

## What this does not prove

- These files are not a live capture from this repository.
- An unpinned CLI version is not a binary that emitted the file.
- The linux gate does not execute the Windows-only shim test.
