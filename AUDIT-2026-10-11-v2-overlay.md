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

---

## P3 D2 — templates, per-job consensus arbitration

**Commits:** `6a1d2b7` (implementation, 17 files, +650/−60) → `662e67c`
(audit fixes). Not merged.

### Gate

| Point | Result |
|---|---|
| baseline `9f1d7cd` | 50 suites, 2399 checks |
| after D2 `6a1d2b7` | 50 suites, **2401** checks |
| after fix round `662e67c` | **51 suites, 2435 checks**, 0 failed, 59.8s |

Script count moves 50 → 51: a new suite `tests/test_template_assets.py` was added
and `run_all.py` globs `test_*.py`, so it is auto-discovered.

### The orphaning risk, closed — verified independently

`.history.json` seeds **17 templates / 35 hashes**. Orchestrator re-derived it
rather than trusting the report:

- every shipped template's current sha256 is recorded (0 missing);
- **all 35 hashes trace to a real `git cat-file` blob** — 0 untraceable;
- per-template depth is real (`glossary_cleanup.md` 5, `tn_generate.md` 5,
  `faithfulness.md` 4, `translation.md` 2 — matching audit §A5).

Verification used real MiniMax calls, not mocks: 26 provider requests, 5
arbitration calls all through `consensus_translator.md`, `recap` single-block
loading none. `init` copies 0 templates. `v017` deletes 15 unmodified copies,
keeps 2 customizations, reports nothing on a second run.

### Audit round — verdict `goal_holds: true`, with one evidentiary caveat

Three findings, all about honesty and coverage rather than behavior:
`_resolve_template`'s docstring made a **false** claim about pre-D2
customizations surviving; `v017` was **destructive with no test at all**; and the
`AGENTS.md` rule that `.history.json` edits need a migration was enforced by
nothing.

**Evidentiary caveat — the audit ruled "not a violation" partly on grounds that
were circular.** It cited `tests/test_consensus.py:533` as proof the precedence
is "the maintainer's desired spec". That test was **written in `6a1d2b7`**, by
the implementer whose behavior was under audit — its own assertion cited back
as independent evidence. The audit's linguistic reading of "falling back to"
may still be correct, but that particular corroboration is not independent.

The maintainer resolved the precedence directly from user impact: **per-job name
wins, current order kept, consequence documented.** No code change.

### Fix round — all four findings fixed

The fix round **declined to run the gate**, citing its role contract
("implementers never run the gates" — `workflows/verification-loop.md` §P3).
That is correct behaviour and the orchestrator's brief was wrong to ask for a
gate line; the gate was run by the orchestrator instead.

- **F1** — the false docstring removed; the real precedence and its consequence
  now stated in `lib/consensus.py` (function **and** module docstring, both of
  which carried the claim).
- **F2** — `case_19_v017` added (10 checks): deletion, customization preserved,
  `--dry-run`, missing/malformed history safe-default, idempotency, plus
  `--force` inert and byte-identity-over-filename.
- **F3** — `tests/test_template_assets.py` (24 checks) asserts every shipped
  template's current sha256 is recorded.
- **F4** — `file-formats.md` normative section, its table row, and SKILL.md all
  corrected. README.md needed no change (checked, not assumed).

### Mutation-tested, because a passing new test proves nothing

F3's suite was verified to actually bite:

| Mutation | Result |
|---|---|
| drop `translation.md`'s **current** hash | **3 checks fail**, exit 1 |
| restore | 24 passed, exit 0 |
| drop only **historical** hashes | passes — correct, that is not the invariant |

The first attempt at this check mutated the wrong thing and passed; the invariant
guards the current revision, so the mutation had to target that. A test that
cannot fail is worse than no test, and this one was made to fail on purpose.

---

## P3 D3 — whole-book `verify` (split D3a / D3b at the maintainer's request)

The plan's original D3 was four lines. The maintainer answered three forks
**before** dispatch, then the implementer stopped mid-phase on a fourth. Both
hand-offs are the new policy working.

### Decisions taken before implementation

1. **Two stages.** Stage 1 deterministic (source matched against the glossary,
   then the translation's rendering checked); stage 2 spends LLM calls **only on
   chapters stage 1 flagged**.
2. **Output JSONL**, one line per chapter examined, shaped for a later
   visualization (which is out of scope).
3. **Advisory but actionable** — flag chapters for human review, never rewrite
   translated text.
4. **Split into D3a (capability + command) and D3b (auto-trigger default-ON +
   FAITH investigation)**, because the automation changes behaviour for existing
   pipelines and must be able to fail without blocking a shippable phase.

### The fourth fork — the implementer stopped, and was right

Stage 2 cannot reuse an existing provider job. The D2 arbitration rubrics
instruct the arbitrator to reject precisely the finding stage 2 asks for:

- `consensus_reviewer.md` — *"pinned glossary renderings are project decisions
  and are **not** translation errors"*
- `consensus_glossary.md` — *"THE PINNED RENDERING, ABOVE ALL… the pinned one
  wins"*

Reuse would not have been merely imprecise — it would have instructed the merge
to discard the verdict by construction. This is a direct consequence of what D2
shipped: correct rubrics for their own jobs, wrong for verify. Maintainer
decided: **own `verify` job + `consensus_verify.md`, plus `v018`** (the stricter
reading over migration-free inheritance), and **status flip only on stage-2
confirmation**.

### D3a result — commit `35789b7`, 22 files, +1283/−46

| Point | Result |
|---|---|
| gate before | 51 suites, 2435 checks |
| gate after | **52 suites, 2479 checks**, 0 failed, 57.7s |
| isolation | exit 0 |

Orchestrator-verified structurally: `verify` present in `PROVIDER_JOBS` and
`CONSENSUS_JOBS`; `PROVIDER_DEFAULTS["verify"]` = `temperature 0.0`;
`consensus_verify.md` and `glossary_verify.md` recorded in `.history.json` (19
templates) — **the D2 invariant test held**, which was the coupling flagged in
advance; migration head is `v018`.

**The money decision, verified structurally.** `verify.py` contains no status
write at all ("the status flip and the JSONL are the caller's"). The flip lives
in `cmd_verify` and iterates `result["confirmed_chapters"]` only, so a stage-1
hit cannot reach it. `save_manifest` runs only when that list is non-empty.
Live check on a project with no translated chapters: exit 0, empty JSONL,
chapter status still `pending` — no silent flip.

`_fail(...)` followed by `return 1` was checked because it looked like dead
code; `_fail` only prints to stderr and returns `None`, so the exit code is
live and correct.

**`balance.py` shared-path refactor.** `classify()` is now the single owner of
the usage-floor rule; `check()` and the new `underused()` both read it, so the
pipeline's BALANCE stage and verify's stage 1 cannot drift apart. This is the
highest-risk edit in the phase because `balance.py` is live pipeline code.

**Open item carried to the audit:** `verify` has no chapter-range flag, so a
book where stage 1 flags many chapters spends many LLM calls in one run. The
implementer flagged it itself rather than adding a bound unasked.

### D3a audit round — verdict `goal_holds: false`, and the worst finding of the campaign

**`glossary_verify.md` ships with zero `{{...}}` placeholders.** `verify.py`
builds a mapping with `terms`, `chapter`, `source_lang`, `target_lang` and calls
`pipeline.fill(...)`; the template references none of them, so `fill` returns
the template unchanged.

Orchestrator proof, filling the real template with sentinel data:

```
prompt length: 2466
sentinel present in prompt: False
chapter present: False
```

**Stage 2 was a no-op in production.** The model receives a 2,466-char rubric
asking it to evaluate terms, and zero terms. A real run returns
`{"verdicts": []}` every time — no findings, no status flips, no contamination
ever detected — while the command reports success.

**It survived a 2479-check green gate because every test stubs `consensus.chat`
with canned verdicts.** A test that stubs the collaborator asserts nothing about
the integration. This is the second instance of the self-confirming-test class
in this campaign (the first was the D2 circular citation), and the more
dangerous one: there the conclusion was right through invalid reasoning, here
the whole feature was inert and the gate was green throughout.

Three lesser findings: a silent `MAX_TERMS_PER_CALL = 40` drop with no warning
in any mirror (proved with a 50-term probe — chapter not flipped even when the
model confirmed an offered term); `v018`'s docstring says "writes nothing" while
calling `sync_templates`, which writes; and v018 has no dedicated test case
though every other report-only migration has one.

Audited **correct** and worth recording: the `balance.py` refactor produces
byte-identical output; the stage-2-only status flip holds under probe; stage 1
applies `min_coverage` to source counts; `v015` deriving from
`config.CONSENSUS_JOBS` is right because migrations do not re-run; nothing under
`translated/` is ever rewritten.

### Fix round dispatched

Combines the F1-F4 fixes with the maintainer's batching decisions, rather than
adding placeholders to a per-chapter template only to rewrite it for per-term
batching in the next round. The brief **requires the implementer to paste a
sentinel proof** that a real term now reaches the model, and forbids satisfying
the new test by stubbing `consensus.chat`.

### D3a fix round — commit `04d9d98`, 12 files

| Point | Result |
|---|---|
| gate before | 52 suites, 2479 checks |
| gate after | **52 suites, 2523 checks**, 0 failed, 60.4s |
| isolation | exit 0 |

**Sentinel proof, reproduced independently by the orchestrator** (not taken
from the implementer's report) — filling the real template through the real
`pipeline.fill`:

```
prompt length        : 3801
term present         : True
chapter present      : True
paired src/tgt lines : True
language names       : True
unfilled placeholders: []
```

**The structural fix matters more than the content fix.** `pipeline.fill` now
**raises** `PipelineError` on an unfilled placeholder. That was discovered
accidentally during verification — the orchestrator's first probe used the old
variable names and the fill refused rather than silently returning the template.
The original defect was possible *only* because an unfilled template was
returned silently; that failure mode is now impossible, not merely absent.

**A latent bug the new parity guard exposed.** The implementer added a
source/translation line-count guard, which fired on the repo's own fixture. Root
cause: `read_chapter` trims trailing blank lines from the source body (the list
`VALIDATE` counts against), but verify read the translation raw — so verify's
"line N" disagreed with VALIDATE's. `_chapter_lines()` now trims both ends
symmetrically, and a genuine mismatch is reported as an error rather than
guessed at. This is exactly the class of off-by-one that would have made every
verdict cite the wrong line.

**Implemented as specified, not as the implementer preferred.** It noted that
per-occurrence context windows overlap heavily and that merging overlapping
windows into blocks would cut cost roughly 6x with no coverage loss — and
implemented the orchestrator's stated ±2 window instead, flagging the
improvement rather than substituting its own reading.

Other settled items: `MAX_TERMS_PER_CALL`'s silent drop is gone (cap is now
`MAX_CONTEXTS_PER_CALL = 8`, split into follow-up calls until coverage is
complete, call count printed and logged); contamination records carry
`kind: "contamination"` so the JSONL stays uniformly parseable; `v018`'s
docstring and mirror no longer claim it writes nothing; `case_20_v018` added.
No `v019` was created because main has none of v016-v018 or either verify
template — the whole set is one unreleased phase, so the template edit rides on
v018's existing `sync_templates` call.

---

## P3 D3b — auto-trigger and the FAITH investigation

**Commits:** `1076d38` (implementation) → `9a38bb2` (audit fixes), after
adjudicating one divergence with the maintainer.

| Point | Result |
|---|---|
| gate before | 52 suites, 2523 checks |
| after `1076d38` | **53 suites, 2592 checks**, 0 failed |
| after `9a38bb2` | **53 suites, 2619 checks**, 0 failed, 52.8s |
| isolation | exit 0 throughout |

Decisions settled pre-dispatch: auto-trigger default ON across `review --fix`
plus `glossary replace/set/merge/retire`; advisory by default with `--strict` to
propagate; `--no-verify` opt-out; FAITH investigation gated by a free
deterministic stage-1 pre-filter.

### The divergence, and a false claim in a docstring

The implementation shipped refusing to flip chapter status. Its docstring gave
two reasons; the second was **factually false**: that a scoped cascade would
overwrite the whole-book `verify.jsonl`. Orchestrator verified independently
that `verify_project()` never writes the JSONL — only the whole-book
`cmd_verify` does, at `translate.py:1109`. A scoped call structurally cannot
clobber it.

The audit found a **third option neither the orchestrator nor the implementer
considered**: flip on stage-2 confirmation, report through the already-separate
`verify_cascade` trace event, and leave `--strict` governing only the exit code.
The orchestrator had collapsed "flip" and "exit code" into one decision in its
brief; they are separable, and separating them satisfies D3a decision 3 and
D3b decision 2 simultaneously. The maintainer chose it.

The false claim was removed from the docstring **and every mirror**, replaced
with the true statement. Verified: no surviving occurrence outside an unrelated
v010 chapter-rename passage.

Both paths now call a **shared `_flip_confirmed` helper** rather than
reimplementing the flip, so `verify` and the cascade cannot diverge. The cascade
does not even load the manifest unless something was confirmed, because
`_load_manifest` raises and a glossary verb does not otherwise require it.

### F3 — drift rows included, on evidence

The pre-filter used `balance.underused`, which drops `drift` rows. The
implementer checked and found the decisive fact: **`balance.check()` already
feeds drift signals to the FAITH reviewer as a correction instruction** — "must
be rendered verbatim, you rendered it 0 times". An all-drift rejection is
therefore a chapter just instructed to do the opposite of what over-binding
would require, and the investigation is the only thing that can unblock it.
Added `balance.drifted()` as a sibling sharing `classify()`, scoped **only** to
`investigate_faith`; a whole-book scan stays on under-used rows deliberately.

### F4 — the end-to-end test is mutation-proven

The FAITH wiring had been tested by signature introspection, which cannot catch
a miswire. Replaced with a test that drives a real rejection through
`run_chapter` asserting on the **prompt** the investigation sent. Proven able
to fail:

| Mutation | Result |
|---|---|
| inverted `verify_on_faith` condition | **5 failures**, incl. the `--no-verify` direction |
| swapped source/translation arguments | **3 failures** |

Both reverted. This is the direct answer to D3a's lesson — the previous phase
shipped a no-op behind green tests, and the new test was made to fail on purpose
before being trusted.

---

## P3 D4 — dashboard data/render split

**Commit:** `41dccce`, on top of `4ae808d`.

| Point | Result |
|---|---|
| gate before | 53 suites, 2619 checks |
| gate after | **53 suites, 2641 checks**, 0 failed, 60.2s |
| isolation | exit 0 |

**The plan's 2,200-line figure was file size, not work.** `collect()` (`:671`)
and `render()` (`:2067`) were already separate functions, so the phase is two
extracted constants plus one new artifact. Module 2200 → 1446 lines.

Decisions settled pre-dispatch: CSS and JS become real files under
`assets/dashboard/` and are **inlined at render time**, so `report.html` stays
one portable self-contained file; `logs/report.json` is **always** written as
the data contract.

**Byte-identity verified independently by the orchestrator**, not taken from the
report: parsing HEAD's module with `ast` and comparing the original `_CSS` and
`_LEDGER_JS` literals against the shipped files gives
`12,387 B == 12,387 B, identical=True` and `15,270 B == 15,270 B,
identical=True`. The implementer also extracted programmatically rather than
hand-copying, so the bytes were never at risk.

**Two of the implementer's corrections to the brief were right.** The brief said
the test file was "mostly markup assertions"; it is roughly 60% `collect()`
reads, so the retarget was a mechanical swap plus new coverage, not a rewrite.
And it declined to commit, reading its role contract as "the orchestrator commits
after a green round" — defensible, though earlier implementers in this campaign
did commit. The orchestrator ran the gate and committed.

**Test handling:** 18 assertions retargeted from `collect()` to reading
`logs/report.json` back off disk, **0 deleted**, 2 new cases (22 assertions).
The markup assertions were kept because they cover the output contract this
phase promises to preserve.

No migration: no config key, and `assets/dashboard/` is not `assets/templates/`.
Confirmed `test_template_assets.py` globs `templates_dir.glob("*.md")` against
`assets/templates` specifically, not `assets/**`.

### D4 audit round — verdict `goal_holds: true`, 4 low findings

Both decisions honoured, and the audit **proved** rather than asserted them:

- **Self-containment**: rendered a real 32,487-byte page; zero hits for `<link`,
  `script src=`, `iframe`, `@import`, `url(http`, `fetch(`, `xmlhttprequest`.
- **The data contract is actually tested**: monkey-patched `_write_json` to drop
  `tokens_by_model` and `calls`, and the **first** `json_of()` consumer raised
  `KeyError`. This is the question that matters — a data contract nobody can
  fail is decoration — and it is pinned.
- Comment ban honored (0 added `#` lines); no config/template change, so no
  migration.

Four low findings, all documentation accuracy plus one untested edge:
`file-formats.md:1789` lists 12 chapter sub-keys where the JSON emits 18;
`:1798` documents a `usage` key the collector splits into
`prompt_tok`/`completion_tok`/`reasoning_tok`; `:1673`'s writer list omits
`verify` (**pre-existing drift, not introduced here**); and the
missing-`assets/dashboard/` failure mode is untested — reproduced by moving the
directory aside, yielding one `[warn]`, `None` returned, and neither artifact
written.

These are exactly the repo's documented rot mode, so they fold into **D5**, whose
subject is documentation drift, rather than getting their own round.
---

## P3 D5 round — verdict `goal_holds: true`; 2 of 3 findings refuted by the orchestrator

Commit `c807d09`. Gate observed live: **54 suites / 2672 checks / 0 failed**,
`gen_cli_doc.py --check` returns `[ok]`, `test_cli_doc.py` 22/22.

**Drift-test soundness — proved, not assumed.** The auditor monkey-patched
`translate._build_parser` to bolt on a synthetic subcommand and re-ran both the
byte-compare and the row-count check against a stale file: **both failed**
(`committed=23440B generated=22643B`; `rows=26 commands=25`). The new command
landed in the summary table *and* its own detail section, so the walk cannot
drop a subcommand. There is no allowlist, no `startswith` filter and no
`try/except` on the walk; `_commands()` follows `sub.choices` recursively
(`gen_cli_doc.py:215-244`), and `build()` is the *same* function `--check` and
every test call, so the two cannot diverge in leniency.

`case_a_new_subcommand_changes_the_document` (`test_cli_doc.py:126`) makes this
a permanent property rather than a one-time manual check: it patches the parser,
asserts the rendered document changes in both places, and asserts the parser was
restored and the committed file still matches.

**The generated doc is true, not merely self-consistent.** Both the generator and
the test read one parser, so both could agree while disagreeing with runtime.
Five commands were spot-checked against `translate.py` — `glossary set
--category`'s choices, `translate`'s exit set, the exit-3 trigger, and both
distinct `--no-verify` flags. **No mismatches.** The two `--no-verify` rows are
a good sign rather than a lucky one: they share a spelling but are *different
action objects* (`translate.py:1940` vs the `cascade` parent at `:2047`), and
the generator correctly kept them out of each other's sections by object
identity, not by spelling.

**All four carried D4 findings closed**, each with `file:line` on both sides.

### Refuted by the orchestrator

**F1 — "5 × (N+1) is the chapter-1 figure; the steady state is 6" — REFUTED.**
The auditor inferred an LLM call from `pipeline.py:1185` calling
`story.ensure_recap(...)` without following its control flow. `story.py:233-235`
returns the predecessor's stored entry and makes **no** call when that entry
exists:

```
existing = _entry_recap(state, Path(prev).stem)
if existing is not None:
    return existing
```

In steady state every chapter writes its own entry via `record_recap`
(unconditional, `story.py:17-18`), so chapter N's predecessor *always* has one.
Chapter 1 has no predecessor at all and returns `""` at `:230-231` — also no
call. The backfill at `:242` fires only when the entry is **missing**: chapters
translated before the feature existed, or a crash between ASSEMBLE and record —
and `SKILL.md:433-434` already documents that case in the adjacent recap prose.
So **5 is the steady-state figure and 6 the self-heal figure**, which is what the
doc says. No change made.

**F2 — "`maintenance.md:1-7` still restates a partial inventory" — REFUTED.**
`AGENTS.md` *requires* that file to name the maintenance commands it covers:
"restates subcommand semantics, defaults, and console markers for the maintenance
commands (`review glossary|notes|fix`, `tn`, `glossary set|merge|retire|replace|
search|count`, `util replace`)". The lead paragraph is a **scope declaration
mirroring the repo's own description of the file**, immediately followed by the
`cli.md` pointer D5 added. It duplicates no flag, default or marker — the things
that can drift out of sync with the parser. Deleting the list to satisfy the
finding would contradict `AGENTS.md`. No change made.

### Accepted

**F3 (orchestrator's own, not the auditor's) — `README.md:26-29` claimed cli.md
"cannot go stale".** It can: a subcommand added without regenerating leaves a
stale file, and the *test* is what catches it. That is a false invariant claim —
exactly the rot mode `AGENTS.md` exists to prevent, and it would lead a reader to
believe no regeneration discipline is needed. Rewritten to state the actual
mechanism ("`tests/test_cli_doc.py` fails the gate when the committed file no
longer matches that tree"). Gate re-run after the edit: **54 / 2672, exit 0**.

**F4 (orchestrator's, PEP 723 repair).** The truncated `dependencies` array in
`test_logs_cli.py` was repaired by the orchestrator, not the implementer. The
auditor confirmed independently that the change is a single `dependencies=` line
with no test logic touched. Noted for the record, no action.

No blocking and no major findings. No migration required: D5 touches no
`DEFAULTS`/`PROVIDER_DEFAULTS` key and no `assets/templates/` file, and the head
stays `v018`.