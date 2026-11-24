# Public release runbook — fresh history

Recursion is published from a clean, single-commit history that contains only the Recursion
product. The private history — which retains removed workstreams, private content, and local
paths — is never published.

Agents must not rewrite history (`AGENTS.md` rules 1 and 4). The commands below are run by the owner.
The push is irreversible for the destination repository; do it deliberately.

## Preconditions

- You are on the review branch you intend to publish. (The history-pruning decision is D-017 in
  `state/DECISIONS.md`; this branch does not assume it performed the pruning.)
- The destination repository exists and is **empty** (no README, no license, no .gitignore).
- You accept that this publishes whatever is in the working tree below, minus the exclusions.

## 1. Build the clean history locally

```bash
# from the repository root
git status --short                 # review: deletions + amendments
git checkout --orphan public/recursion-v3
git add -A
```

```bash
git commit -m "Recursion v3 — initial public release"
```

## 2. Verify — all must hold

```bash
git rev-list --count HEAD          # exactly 1
git log --oneline                  # exactly one commit
git status --short                 # clean
```

Confirm the scope gate passes, then:

```bash
python -X utf8 -B tools/check_public_scope.py
```

Secret scan over the exact published tree:

```bash
git grep -nIE "sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|(password|passwd|api[_-]?key|client[_-]?secret)[[:space:]]*[:=][[:space:]]*[\"']?[A-Za-z0-9/_-]{12,}" || true
```

## Caveat — the history leak remains

On this tree the scope gate is clean: `tools/check_public_scope.py` passes and no known foreign
identifier remains in the published text. The private git history is the only remaining
publication concern — a fresh history fixes that **history** leak, but it is not fixed by
scrubbing `HEAD` alone.

Do not publish until the scope gate passes and the secret scan is empty.

## 3. Publish

```bash
git remote add public <NEW_EMPTY_REPO_URL>
git push public public/recursion-v3:main
```

Then review the public repository as a fresh visitor before announcing it.
