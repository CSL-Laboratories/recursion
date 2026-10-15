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
| `claude-auth-status.txt` | source-documented | Claude Code 2.x, as that source names it |
| `claude-auth-status-exit-1.txt` | synthetic | unpinned; changelog head on 2026-10-15 was 2.1.299 |
| `claude-stream-json.txt` | synthetic | unpinned; same changelog note |

`# evidence: synthetic` is the synthetic mark. Every file has `# cli-version:`.
A published line that is not valid JSON is omitted, and the header says so.

## Parsers

`scripts/tests/v3/harness/harness.test.py` loads these files. The verdict and
usage parsers read the stream fixtures. The doctor parser reads the Claude
auth-status fixtures, including exit 1 with `loggedIn: true`.

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
