# AUDIT — `PLAN-2026-10-11-v2-overlay.md` (P1, single static round)

**Date:** 2026-10-11
**Plan audited:** `PLAN-2026-10-11-v2-overlay.md` rev 0
**Method:** four parallel adversarial lenses (`vt-auditor` on `minimax/MiniMax-M3`,
model passed explicitly per `workflows/verification-loop.md` §1 caveat 1), one
per attack surface, followed by orchestrator re-verification of every load-bearing
finding at source level.
**Repo at `913dc01`.** No files were changed by the audit.

**Reference:** the plan's **pinned functional goal** and `AGENTS.md` — never the
plan's internal consistency.

| Lens | Findings | Result |
|---|---|---|
| goal-testability | 3 high / 4 med / 1 low | goal needs amendment |
| goal-achievement | 6 med / 4 low | three real holes |
| feasibility | 2 med / 8 low | one concrete crash |
| convention-poisons | 1 high / 6 med / 1 low | AGENTS.md carve-out |

**Verdict: REWORK.** The layering model is sound and the phase decomposition is
right, but the goal is not yet decidable (8 goal-testability findings), one
phase crashes `init` as written, and two claimed mechanisms do not exist.

---

## Confirmed findings and dispositions

Each was re-verified by the orchestrator at source before disposition. Where an
auditor's claim did not survive, it says so.

### A1 — `translate.py:402` crashes `init` — HIGH, **accepted, blocks D1**

Feasibility lens. Removing `cfg.update(config.DEFAULTS)` (plan D1) leaves the
local `cfg` dict without `seed_min_count`; `config.get_number` has **no**
DEFAULTS fallback and raises.

> **Verified:** `translate.py:402` — `config.get_number(cfg, "seed_min_count")`
> reads the local dict. `config.py:541` — `if key not in cfg: raise ValueError(
> f"config key '{key}' is missing")`, docstring: *"No DEFAULTS fallback: callers
> reading a load_config() result always find the key."* `cmd_init` reads the
> local `cfg`, not a `load_config()` result.

**Disposition:** D1 must re-load through `_load_config(project_dir)` after
`save_config`, or use the existing `cfg.get(key, DEFAULTS[key])` pattern already
present at `translate.py:418-419`.

### A2 — v016 has no sentinel for `base_url` — HIGH, **accepted, blocks D1**

Convention-poisons (high) and goal-achievement (medium) converged. Once D1
deletes `DEFAULT_BASE_URL`, every existing project's provider blocks still hold
the old LAN string. Stripping it leaves no endpoint (hard failure at first
call); leaving it silently points at a dead host.

> **Verified:** `config.py:37` `DEFAULT_BASE_URL = "http://100.85.218.125:8888/v1"`
> is inherited by all 7 blocks (`:45-51`). `v009`'s docstring states the
> sentinel caveat verbatim: *"a user who genuinely wanted 8192 cannot be
> distinguished from one who never touched it"* — and accepts it, because the
> stripped value still resolves through layer 0. **`base_url` is the one field
> for which that safety argument does not hold**, because D1 deletes its layer-0
> value.

**Disposition:** v016 strips `base_url` from project blocks **only** where it
equals the retired LAN constant, and reports each stripped job as `[warn] ... set
providers.<job>.base_url (or config.local.json)`. Load-time validation then
names the job. Not silent, not fatal-at-first-call.

### A3 — layer-1 is bypassed by two raw readers — MEDIUM, **accepted**

Goal-achievement. `logger._resolve_flags` and `vcs._enabled` read raw
`config.json`, so `config.local.json` never reaches them.

> **Verified:** `logger.py:127` → `_raw_config(project_dir)` → raw
> `json.loads` (`:102`), never consulting `config.local.json`.
> `vcs.py:85-91` same shape for `git_commits`.

**Disposition:** D1 routes both through the layered loader. The logger's
"read once per invocation" performance rationale is preserved (the layering
composes once, then caches).

### A4 — `apply_overlay` materializes layer 1 at init and `sync-config` — MEDIUM, **accepted**

Goal-achievement. Under read-time layering these two call sites are exactly the
materialization the goal forbids, and they make later edits to
`config.local.json` silently lose to the stale project value.

> **Verified:** `load_local_config` is called **only** at `translate.py:307`
> (`init`) and `translate.py:503` (`cmd_sync_config`); both then call
> `apply_overlay` (`:316`, `:519`), which writes layer 1 **into** the project
> file.

**Disposition:** D1 removes both `apply_overlay` call sites. `sync-config`
survives, redefined as a *report-only* "here is what your layers resolve to"
command (it keeps its exit-code contract for a malformed overlay).

### A5 — `.history.json` seed orphans pre-D2 projects — MEDIUM, **accepted**

Goal-achievement and convention-poisons converged. Seeding only *current*
hashes classifies every pre-D2 project's identical copy as a customization, so
prompt improvements never reach them — a direct goal violation.

> **Verified:** `assets/templates/` ships 11 templates; the mechanism does not
> exist yet. Historical revisions **are** recoverable: `git log --follow` on
> `translation.md` returns 2 distinct blob hashes (`a6e2040b1c5e`,
> `d666c437a1c2`), so the seed can be built from git history at authoring time.

**Disposition:** v017 seeds `.history.json` with **every shipped revision's hash
recoverable from git history**, not just the current one. This is a one-time
authoring action; the file ships in the skill.

### A6 — goal is ambiguous in six places — HIGH, **accepted, blocks P3**

Goal-testability. The goal text admits two honest readings on: template layer
count (goal says 3, plan implements 2), the AGENTS.md carve-out, the v016/v017
transition carve-out, "what its author wrote" at `init`, "file-wise" vs the
hash fall-through, and whether a per-job template resolves for single-block jobs.

**Disposition:** the goal is rewritten in §"Pinned goal (rev 1)" below. It now
states the template layer count explicitly, names `CONSENSUS_JOBS`
(`config.py:9`, 6 entries — verified), scopes per-job templates to jobs that
**actually fan out** (`consensus.py:158-162` returns before any template load on
a single block), and carries the transition carve-out.

### A7 — `AGENTS.md` wording must be pinned in the plan — MEDIUM, **accepted**

Convention-poisons. The plan proposes editing `AGENTS.md` freehand, so the D2
audit would compare against a paraphrase. Exact replacement wording is now in
D2 below.

### A8 — `.history.json` has no appender — MEDIUM, **accepted**

Both lenses. `sync_templates` (`common.py:48`) is per-project and never touches
the skill's file; there is no skill-side appender.

**Disposition:** `.history.json` is edited by the **maintainer at authoring
time** (same moment as writing the prompt), and the **maintainer-only**
AGENTS.md rule keeps a migration for any *history-file* change so the append is
recorded. Correctness does not depend on it growing — the fall-through resolves
to the *current* shipped file — but the classification must not silently rot.

### A9 — doc-mirror propagation is enumerated for D1 only — MEDIUM, **accepted**

Convention-poisons. D2–D5 name no mirrors, so each later phase risks shipping
drift. Mirrors are now named per phase in D2–D5 below.

### A10 — "11 of 15 migrations" is inflated — MEDIUM, **accepted as wording**

Feasibility. v005, v006, v010, v014 are template-only or data-layout changes
that would ship regardless; the honest figure is **7 of 15** attributable to
stored state (v002, v003, v004, v007, v008, v009, v011). The plan's *conclusion*
stands; its number is corrected so the claim is defensible under audit.

### A11 — `style.md` is an unlayered prompt body — MEDIUM, **accepted, scoped out**

Goal-achievement. `style.md` is copied from `assets/styles/` at
`translate.py:271-273` and is hand-editable; shipped preset improvements never
reach an existing project. This is the same class as templates, but it is a
**project data shape** (a chosen preset name recorded in `novel_info.json`),
not a template override.

**Disposition:** **out of scope for v2**, named in the plan's out-of-scope
section with its reason, so the goal's phrase "every prompt template" is
narrowed to templates loaded through `_load_template`. Shipping it half-layered
would be worse than not shipping it.

---

## Refuted or downgraded

| Claim | Source | Outcome |
|---|---|---|
| "Exit 3 is unreachable in `translate.py`" | convention-poisons | **REFUTED.** `translate.py:2084-2095` returns `3` on `client.LLMFatal`. The underlying drift (docstring `:10-15` omits exit 3) is **real** and stays as D5 work. |
| "3-layer `merge_overlay` composition has an ordering bug" | raised by me, tested by goal-achievement | **REFUTED.** I ran it: DEFAULTS → local `[A,B]` → project `{model}` yields `[{A, project-model}, {B, project-model}]` — the single-block rule spreads across both blocks by design. Behavior is correct; now stated in the plan. |
| "`_load_template` fall-through breaks a write-then-read caller" | feasibility | **REFUTED.** All 11 call sites are pure reads; `translate.py:370` copies but never reads back. |
| D4 sizes understated | feasibility | Minor drift (CSS 226 vs 228, JS 402 vs 404, markup 563 vs 567). Numbers corrected; no design change. |

---

## What the audit changed

The plan is amended in place. Net effect: **one phase gained three blocking
sub-tasks** (A1, A2, A4 — `init` crash, `base_url` sentinel, overlay
materialization), **two modules gained layering work** (A3 — logger, vcs), the
goal is rewritten to be decidable (A6), `style.md` is explicitly scoped out
(A11), and the migration-count claim is corrected (A10).

Two of these (A1, A2) would have shipped as runtime failures on every existing
project — the class of defect this loop exists to catch before merge.

---

## P2 — isolated checkout bootstrap (2026-10-11)

**Checkout:** `D:/Repos/vibed/translate-skill-wt-v2-overlay`
**Branch:** `scratch/v2-overlay`, branched from the working-branch tip `913dc01`.
**Commit:** `f5d7525` — *"docs: v2 overlay plan + P1 static audit round; track
the verification loop"*, carrying `PLAN-2026-10-11-v2-overlay.md`,
`AUDIT-2026-10-11-v2-overlay.md` and `workflows/verification-loop.md`. All three
were untracked in the main tree and do not travel to a checkout otherwise
(`workflows/verification-loop.md` §P2 bullet 2).

### Vacuous-green probe (§3) — run once for this checkout shape

| Probe | Command | Expected | Observed |
|---|---|---|---|
| nonexistent suite | `uv run <abs>\tests\definitely_nonexistent.py` from `D:\` | exit 2, loud | **exit 2**, `error: Failed to spawn` |
| one real suite by absolute path | `uv run <abs>\tests\test_main_exit_codes.py` from `D:\` | passes | **32 passed, 0 failed**, exit 0 |

### Baseline gate in the checkout

`uv run tests/run_all.py` → **50 passed, 0 failed (50 scripts, 2362 checks)**,
exit 0, **59.4s**. Matches the main-tree baseline (2362 checks), confirming the
checkout is a faithful branch point and not a degraded one.

### Untracked state (§6) — checked, nothing to copy

- `novel-translator/config.local.json` — **absent in both trees.** Nothing to
  copy. **Carry into D1:** the phase touches provider resolution, and this file
  is the only untracked state that can affect it. Whoever implements D1 should
  create one before the round, or state explicitly that D1 is verified without
  layer 1 exercised (the `apply_overlay` removal and the raw-reader fix are then
  only covered by the migrated-project checks, not by a live layer-1 run).
- `probe-artifacts/`, `.zcode/` — absent in the checkout, expected, and no suite
  depends on either.

The checkout working tree is clean at `f5d7525`; the main tree is untouched by
this bootstrap (`scripts/` and `tests/` still byte-identical to `913dc01`).