# eval/: the evaluation runner and task harvest

Lane EVAL's harness for measuring an engine run directly against the same engine run under Recursion.
Nothing here is product code. Read `PROTOCOL.md` first: it says what is measured, how it is scored and
how a result may be worded.

| File | What it is |
|---|---|
| `PROTOCOL.md` | the protocol, frozen with `analyze.py` and `passk.py` by `FROZEN.sha256` |
| `run` | the runner's command (`eval/run --help`) |
| `runner.py` | one suite run per arm: scratch runtime, fresh copy, engine, diff, hidden tests, record |
| `arms.py` | the direct arm (stand-in engine, Claude Code) and the Recursion arm (not built yet: REFUSAL when seams are missing, NOT_BUILT when they are present) |
| `standin_engine.py` | a scripted stand-in engine that edits the work tree, for offline runs; not a measurement |
| `record.py`, `schema/recursion-eval-run-1.json` | the `recursion-eval-run/1` record and its checks |
| `passk.py` | pass^k and pass@1 from per-task counts; task-level bootstrap, paired and clustered intervals |
| `analyze.py` | the frozen analysis: headline and valid-runs scores, bounds, intervals, paired comparison, claim wording |
| `suite.py` | the pinned suite manifest (`recursion-eval-suite/1`) |
| `heldout.py` | the held-out guard |
| `harvest.py`, `validate.py` | held-out tasks from merged commits; the fail-on-parent, pass-on-commit check |
| `devset/`, `dev.py` | the 12-task dev set and its pin and check tool |
| `tests/` | the lane's tests (`tests_*.py`, so the repository's gate does not count them as its own) |

Run, from the repository root, with the scratch folders the lane preamble names:

    python -m unittest discover -s eval -t .
    eval/run --suite dev --arms direct,recursion --k 5 --engine standin
    python -m eval.analyze eval/results/dev/<record>.json
    python -m eval.dev check

Standard library only. Records go to `eval/results/<suite>/` and trial artifacts only where `--artifacts` says;
neither is committed (`eval/.gitignore`): publishing any record is the owner's decision.
