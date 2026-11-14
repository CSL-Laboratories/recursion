# tools/release

Lane RELEASE's tools: the hash lock on what hosted CI installs, and the CI changes it
proposes as patches. Run everything from the repository root. Stdlib only.

| File | What it is |
|---|---|
| `hash_lock.py` | The check. `python -X utf8 -B tools/release/hash_lock.py` exits 1 unless every requirements file a workflow installs (as the workflows stand, and as the patches in `proposals/` would leave them) is exactly pinned, carries the hashes recorded for it, pins every dependency, and has a wheel for every job that installs it. Offline. |
| `ci-lock.json` | The record the check reads: for each pin, every file PyPI publishes for that version with its sha256, the release's Requires-Python and its dependencies. |
| `relock.py` | Rewrites `requirements-ci.txt`, `requirements-ci-314t.txt` and `ci-lock.json` from PyPI's JSON API, then runs the check. It never changes a version: edit the version first. Needs the network. |
| `requirements-ci-314t.txt` | cryptography and its dependencies, hash-locked, for the proposed free-threaded 3.14 job. |
| `hash_lock_checks.py`, `proposal_checks.py` | The tests (`python -X utf8 -B tools/release/hash_lock_checks.py`). Not named `test_*.py` until `proposals/ci-gate-release-tests.patch` lets the gate run them. |
| `timings.py` | Per-test wall times of one test file, slowest first. |
| `proposals/*.patch` | Changes to frozen files (`.github/`, `tools/`), applied only by Lucas. Each starts with what it does and how to apply it. |

The patches, each independent of the others (any order):

- `ci-314t.patch`: a `core-314t` job running the core suite on free-threaded CPython 3.14 with the GIL off and cryptography installed; the required `linux` check needs it.
- `retire-review-workflows.patch`: retires the Codex gate and Claude fixer workflows (grill Q15).
- `ci-gate-publish-timeout.patch`: 300 s for `tools/test_claude_fixer_publish.py`, which timed out at 180 s on hosted macOS.
- `ci-gate-release-tests.patch`: adds `tools/release/test_*.py` to the gate and renames the two test files to match.

To check a patch before applying it: `git apply --check tools/release/proposals/<name>.patch`.
