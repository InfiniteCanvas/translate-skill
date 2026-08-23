# `retry --failed`: retry every needs-review chapter

## Behavior

`retry` gains a `--failed` flag (mutually exclusive with `--chapters`, one required): it selects every manifest chapter with status `needs-review` — i.e. the ones that exhausted `max_attempts` — in chapter order, and runs the existing retry flow unchanged (wipe draft/translated artifacts → status pending → `run_range`, which also fires the auto-epub path). Empty selection prints `[ok] no needs-review chapters to retry` and exits 0. Pairs with `status --why` (inspect the failure feedback, fix the cause, then `retry --failed`).

Note: chapters *hand-marked* needs-review via `mark` are included — status is the selector, attempts aren't inspected. That matches "failed chapters" as the system defines them and keeps it a pure convenience selector; `retry --chapters SPEC` remains for explicit picks.

## Changes

1. **`scripts/translate.py` — `cmd_retry` (~line 540)**: branch the file selection —
   ```python
   if args.failed:
       files = [e["file"] for e in sorted(manifest, key=lambda e: int(e.get("order", 0)))
                if e.get("status") == "needs-review"]
       if not files:
           print("[ok] no needs-review chapters to retry")
           return 0
   else:
       files = pipeline.parse_range(args.chapters, manifest)
   ```
   Rest of the function (wipe loop, run_range, summary, exit codes) untouched.

2. **Argparse (~line 682)**: `--chapters SPEC` and `--failed` become a mutually exclusive required group (same pattern `translate` already uses for `--chapters`/`--next`).

3. **Docs**: SKILL.md needs-review handling (retry --failed as the bulk path) and README command cheat sheet — one line each.

## Verification

1. `py_compile` + `retry --help` shows both flags.
2. Mock regression: temp project (fixture chapters, mock server) → `mark --chapters 1 --status needs-review` → `retry --failed` retranslates **only** chapter 1 (others skipped as already translated), exit 0; a second `retry --failed` prints the empty message and exits 0.
3. Sync installed copy, commit, push.