Downgrade the balance gate to advisory, hard-failing only on the genuine drift signal.

## Semantics after the change (per handoff + your instruction)

| Check | Old | New |
|---|---|---|
| `src >= 2` and zero canonical renderings | hard fail | **hard fail (unchanged)** — the one reliable drift/omission signal |
| Below usage floor (`< ceil(min_coverage × src)`) | hard fail | **advisory warning** — console `[warn]` + trace log, never injected into retry feedback, never blocks |
| Over-count ceiling (`extra > max(2, src)`) | hard fail | **trace-log only** (no console) — most false-positive-prone check (shared-rendering contamination, generic English words), demoted per handoff recommendation |

`src == 1` with zero renderings becomes advisory (fixes the "amplification by growth" class: single-occurrence seeded terms no longer hard-fail).

## Changes

**1. `scripts/lib/balance.py` — `check()` rewrite**
- Return type becomes `(failures: list[str], warnings: list[str], info: list[str])` instead of `(ok, issues)`.
  - failures: zero-rendering cases (`src >= 2 and tgt == 0`) — message names it as drift/omission.
  - warnings: floor breaches (excluding the hard-fail case) — e.g. `"'前辈' rendered 1/6 times (floor 2)"`.
  - info: over-count ceiling breaches — e.g. `"'plot' rendered 9/2 times (ceiling 4) - possible count contamination"`.
- `count_in_target()` and the stemmer untouched (matching mechanics unchanged — the data-side curation in b61303a handles those).
- Docstring updated to document the three-tier semantics.

**2. `scripts/lib/pipeline.py` — BALANCE stage block (lines 791-809)**
- Unpack three lists; only `failures` extend `state["feedback"]` and set `failed_stage = "BALANCE"`.
- Warnings: print up to 5 per chapter as `[warn] balance advisory: ...` (+ `... and N more (see logs)` overflow line) and log one event: `{"event": "balance_advisory", "chapter": ..., "warnings": [...], "over_count": [...]}` via `logger.log_event` (unconditional, like attempt events).
- Info: log only, no console.
- Exception path (`BALANCE check failed: ...`) unchanged — code errors remain retryable failures.

**3. Docs**
- `references/file-formats.md`: rewrite the balance paragraph — hard fail = zero renderings with src ≥ 2; floor = advisory (console + trace log, `min_term_coverage` still tunes it); over-count = trace-log only. Mention the trace-log event name for greppability.
- `SKILL.md` pipeline step 4 (BALANCE): same wording, one paragraph.
- `README.md` tuning line: `min_term_coverage` now described as the advisory floor.

No new config keys; no state-schema changes (advisories live in the trace log; state files die on success anyway).

## Verification

1. Unit checks against the new `check()` (python -c): zero-render drift still fails; floor breach warns without failing; over-count lands in info only; src=1/zero warns.
2. Full offline regression (mock server): init → translate → epubcheck green.
3. **Live proof of the fix**: fresh mini-project from 2 sotn chapters seeded with the new 314-term catalogues (which co-seed shared renderings like 前辈/师兄 — the contamination class from the handoff), translate 1 chapter against the live endpoint, confirm: no needs-review from balance, advisories visible in console + `logs/llm-*.jsonl`.
4. Commit to main, push to origin, sync `~/.zcode/skills/novel-translator`.

## Out of scope (per you)
- Matching-mechanics improvements (inflection blindness, hyphenation, phrase rigidity) — catalogue curation covers these data-side.
- Style tweaks (unease et al.).