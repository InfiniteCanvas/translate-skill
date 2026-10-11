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

### Untracked state (§6) — layer 1 provisioned

`novel-translator/config.local.json` — **created in the checkout only**, copied
verbatim from the shipped `config.local.example.minimax.json` (no hand-written
config, no invented endpoint). Verified ignored via `git check-ignore -v`
(`.gitignore:21`) and absent from `git status`, so it cannot reach a commit.

Fixture project at `%TEMP%\nt-d1-layer1`, two synthetic chapters, `--skip-profile`.

### Pre-D1 baseline — the layering defects, captured live

`init` against the layer-1 overlay, at `913dc01`:

```
[init] wrote config.json
[init] [ok] config.local.json: applied 8 key(s): providers.annotator,
  providers.consensus, providers.glossary, providers.profile, providers.recap,
  providers.reviewer, providers.translator, translate_max_output_tokens
[init] copied 11 template(s) into templates/
```

| Observable | Value | D1/D2 target |
|---|---|---|
| project `config.json` top-level keys | **27** | authored keys only |
| layer-1 values present in the project file | **yes** — `base_url`/`model`/`api_key_env` all point at MiniMax (`config.json:7,8,13`) | gone; read at run time |
| templates copied into the project | **11** | **0** — no copy at `init` |

This is the materialization the pinned goal forbids, observed rather than
inferred. It is the concrete D1/D2 acceptance target.

`ping` against the same project: **all 7 jobs `[ok]`, "all providers
reachable"**, credential resolved from `$MINIMAX_API_KEY`. Layer 1 is therefore
fully exercisable in D1 — the round can verify a real overlay read, not only the
migration path.

The checkout working tree is clean at `f5d7525`; the main tree is untouched by
this bootstrap (`scripts/` and `tests/` still byte-identical to `913dc01`).

---

## P3 D1 — implementation, audit, fix round

**Commits:** `d918504` (implementation, 32 files, +950/−308) → `a518806`
(audit fixes, 7 files). Not merged.

### Gate, run independently by the orchestrator

| Point | Result |
|---|---|
| baseline at `9d53e2d` | 50 passed, 0 failed, **2362** checks, 59.4s |
| after D1 implementation `d918504` | 50 passed, 0 failed, **2382** checks, 52.2s |
| after fix round `a518806` | 50 passed, 0 failed, **2399** checks, 56.8s |

The fix round re-derived the 2382 baseline itself (`git stash` → gate →
`git stash pop`) rather than trusting the reported figure. Net +17 checks, no
regression.

### The goal's central claim, verified by direct inspection

| Observable | Pre-D1 | Post-D1 |
|---|---|---|
| project `config.json` top-level keys | 27 | **3** (`source_lang`, `target_lang`, `version`) |
| `minimax｜MINIMAX_API_KEY｜100.85.218.125` in the project file | 8 hits | **no matches** |
| `ping` on an endpoint-less project | impossible | **all 7 jobs `[ok]` → MiniMax-M3** |

Three-layer composition verified by running it: a project single-object
override preserves layer 1's endpoint and applies its own value; an authored
array replaces wholesale; v016 preserves a deliberate `thinking: true` and
`temperature: 0.42`.

### Audit round (`vt-auditor` on `minimax/MiniMax-M3`) — verdict `goal_holds: true`

4 medium + 5 low. Orchestrator re-verified every medium finding at source
before disposition:

- **M1 CONFIRMED** — `_load_config_lenient` (`translate.py:138-156`) caught
  `(OSError, ValueError)`, reached by 6 commands, while `file-formats.md` and
  `SKILL.md` claimed a malformed overlay "fails EVERY command". Mirrors and
  code contradicted each other.
- **M2 CONFIRMED** — a v16 project with `[{}]` provider blocks and no overlay
  could never run `migrate` again (`cmd_migrate` → `_load_config` →
  `resolves no base_url` → exit 2). A migration requiring the thing it repairs.
- **M3 CONFIRMED** — `cmd_init` wrote directories, `style.md` and a bare
  `config.json`, *then* failed the endpoint check, leaving a half-built project.
- **M4 CONFIRMED** — `SKILL.md`'s migrate walkthrough stopped at v014; v016, a
  breaking change, appeared nowhere.

Refuted by the implementer: the auditor's F8 (`AGENTS.md` rot) — the two
statements already agreed, so no change was made.

### Fix round outcome

F1–F7 fixed; F8 correctly declined. Two findings the implementer surfaced
beyond the brief:

1. **F2b — the same trap one layer deeper, in the primary workflow.**
   `materialize_config` loads without a skill root, so a *healthy* v5 project
   whose endpoint lives only in the overlay died mid-chain at v007. Fixed with
   a documented `require_base_url` flag; shape/JSON/consensus guards intact.
2. **A rejected test deletion was reported honestly** — `case_3_apply_overlay`
   was deleted with `apply_overlay` (F7), while `case_8`/`case_9`/`case_11`
   were re-pointed at the live read path rather than deleted, and two
   assertions changed meaning (post-D1 the project out-ranks the overlay).

### Post-fix verification (orchestrator, live)

| Check | Result |
|---|---|
| F2: v16 `[{}]` blocks, no overlay → `migrate` | **exit 0**, ran to completion |
| F1: malformed overlay + existing project config → `glossary count` | **`[FAIL]` + exit 2**, names the file |
| F3: `init` with neither `--api-base` nor overlay | **exit 2**, leaves only the user's `source/` |
| Regression: normal `init` + `ping` | 3 authored keys, all 7 jobs reachable |

One orchestrator test was itself **invalid on first run**: the F1 fixture
omitted a project `config.json`, so `FileNotFoundError` was correctly tolerated
and the command succeeded. Re-run with a config present, F1 fails as designed.
Recorded because a harness that cannot distinguish "fixed" from "never fired"
is worse than no harness.

### Incident — working-tree contamination, detected and reversed

`git status` in the main tree showed **7 modified test files** despite both
implementers reporting the main tree untouched. Investigation: the session's
first status check recorded only `M AGENTS.md`, and the 7 files were written at
**08:08–08:09**, inside the first implementer's execution window. They carried
D1-derived fixture edits (`{"providers": {}}` → an authored `base_url`) that
were never committed to the branch, and differed from the branch's versions.

Confirmed the authoritative versions were committed and green on the scratch
branch, then `git restore`d the 7 files. Main tree is back to its pre-session
state: `M AGENTS.md` (the user's own edit, preserved) plus the three untracked
plan/audit/workflow docs.

**Lesson for the loop:** "work only in the checkout" is an instruction, not a
guarantee. Every phase boundary should re-check `git status` in the main tree.
The check that caught it was incidental — run to inspect branch state, not to
look for contamination.

### Fix — isolation made checkable (`4b4edae`)

Rather than rely on the orchestrator noticing, `workflows/check_isolation.py`
records the main tree's HEAD plus the porcelain status of `novel-translator/`
and `tests/`, and verifies it at phase boundaries. Exit 0 clean, exit 1
contamination; it also fails on a main-branch advance mid-campaign, which would
invalidate the branch point.

Four behaviours verified live against the real trees:

| Behaviour | Result |
|---|---|
| simulated leak (`Add-Content` on `tests/test_git.py`) | **exit 1**, names the path |
| same leak restored | **exit 0** |
| orchestrator doc writes in the main tree (`AUDIT-probe-temp.md`) | **exit 0** — correctly ignored |
| baseline tampered to a different HEAD | **exit 1**, "main tree HEAD moved" |

Scope is `novel-translator/` + `tests/` only, so the orchestrator's own
`PLAN-*.md` / `AUDIT-*.md` writes in the main tree never false-positive.
Wired into `workflows/verification-loop.md` (§P2 record, §P3 step 0 verify) and
`AGENTS.md`. Gate re-confirmed at 2399 checks after the addition.