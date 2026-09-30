# Public release runbook — fresh history

Recursion is published from a clean, single-commit history that contains only the Recursion
product. The private history — which retains removed workstreams, private content, and local
paths — is never published.

Agents must not rewrite history (`AGENTS.md` rule 4). The commands below are run by Lucas.
The push is irreversible for the destination repository; do it deliberately.

## Preconditions

- You are on the pruned review branch (see D-017 in `state/DECISIONS.md`).
- The destination repository exists and is **empty** (no README, no license, no .gitignore).
- You accept that this publishes whatever is in the working tree below, minus the exclusions.

## 1. Build the clean history locally

```bash
# from the repository root
git status --short                 # review: deletions + amendments
git checkout --orphan public/recursion-v3
git add -A

# Exclude the private, non-public artifacts before committing:
git rm -r --cached --ignore-unmatch docs/v3/archive
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

Confirm no removed workstream content is tracked (the path list lives in the scope tool, not
here), then:

```bash
python -X utf8 -B tools/check_public_scope.py
```

Secret scan over the exact published tree:

```bash
git grep -nIE "sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|(password|passwd|api[_-]?key|client[_-]?secret)[[:space:]]*[:=][[:space:]]*[\"']?[A-Za-z0-9/_-]{12,}" || true
```

## Caveat — the scope gate may still fail

A fresh history fixes the **history** leak, not every identifier remaining in `HEAD`. As of the
scope-prune branch, foreign identifiers still exist in:

- `scripts/**` — control-plane runtime and test fixtures;
- `state/DECISIONS.md` — historical entries (the log's own rule forbids editing them);
- `docs/audits/recursion-v2-phase0-transition-receipts/` — immutable v2 receipts.

A fresh history alone will therefore publish those. Resolve first by either:

- a bounded scrub task for those paths with its own gate; or
- excluding the private control-plane paths from the published tree.

Do not publish until the scope gate passes and the secret scan is empty.

## 3. Publish

```bash
git remote add public <NEW_EMPTY_REPO_URL>
git push public public/recursion-v3:main
```

Then review the public repository as a fresh visitor before announcing it.
