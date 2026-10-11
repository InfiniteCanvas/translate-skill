# PLAN — v2 overlay architecture for `novel-translator`

**Date:** 2026-10-11
**Status:** proposal, not yet implemented. No code has been changed.
**Revision:** **rev 1** — amended after the P1 static round
(`AUDIT-2026-10-11-v2-overlay.md`, 4 lenses, verdict REWORK). The pinned goal is
rewritten to be decidable; D1 gains three blocking sub-tasks and two modules;
D2 pins its `AGENTS.md` wording; `style.md` is scoped out with a reason; the
migration-count claim is corrected. Every change traces to an audit finding.
**Author scope:** `D:\Repos\vibed\translate-skill` @ HEAD `913dc01`.
**Baseline:** `uv run tests/run_all.py` → **50 passed, 0 failed (50 scripts,
2362 checks)** in 62s. Verified live at plan time.

---

## Pinned functional goal — round-invariant

**rev 1** (amended after the P1 static round; every change traces to
`AUDIT-2026-10-11-v2-overlay.md` — see its §A6).

> **Configs resolve in three layers, lowest precedence first:** (1) the code
> defaults `DEFAULTS` / `PROVIDER_DEFAULTS` in `lib/config.py`; (2) the skill's
> gitignored `config.local.json`; (3) the project's own `config.json`. A higher
> layer overwrites a lower one key-wise. Layer 1 is consulted at **read time**
> and never written into layer 2 — a project's `config.json` holds only keys a
> human put there, so editing `config.local.json` changes every project's
> behaviour immediately, with no `sync-config` run and no migration.
>
> **Templates resolve in two layers, lowest precedence first:** (1) the skill's
> shipped `assets/templates/<name>.md`; (2) the project's `templates/<name>.md`.
> A project file **wins** unless its bytes are byte-identical to a
> known-shipped revision of that template, in which case it is an unmodified
> leftover and the skill's copy is used instead. `style.md` is a chosen project
> data shape, not an overlay, and is out of scope.
>
> **Every one of the six jobs in `config.CONSENSUS_JOBS` — `translator`,
> `glossary`, `reviewer`, `annotator`, `recap`, `profile` — that fans out (two
> or more provider blocks) resolves its own arbitration template
> `consensus_<job>.md`, falling back to the general `consensus.md`.** A job with
> a single block makes one call and loads no template, unchanged.
>
> A change to a shipped default or shipped prompt reaches every project on its
> next run **without a migration**; exactly one migration (the D1/D2 transition
> pair) converts projects already written under the materialize/copy model.

Every audit in this campaign tests against the statement above and `AGENTS.md`,
never against this plan's internal consistency.

**Success condition** (from `workflows/verification-loop.md` §2): a phase is
done when its gate ran green in the isolated checkout, the executed code was
adversarially audited against the goal and `AGENTS.md`, and that branch was
fast-forward merged into the working branch and the checkout removed. A phase
that only ever passed a static review is not done.

---

## 1. What is actually wrong today

Three findings, each verified against the code at HEAD.

### 1.1 Project `config.json` is a materialized copy of the code defaults

`migrations/common.py:27` `materialize_config()` reads the project's raw
`config.json`, computes `config.load_config(project_dir)` — which is
`_deep_merge(DEFAULTS, user)` plus normalized provider blocks
(`lib/config.py:228`) — and **writes the merged form back to disk**.

Consequence: a project file accumulates a copy of all 23 `DEFAULTS` keys and
all 7 normalized provider blocks. When a default's *value* changes, every
project file is now stale, which is why the chain needs a step whose only job
is to rewrite it. Reading the 15 `DESCRIPTION` strings:

| Trigger | Migrations |
|---|---|
| new top-level config key | v002, v003, v004 |
| provider job added / reshaped | v007, v008 |
| default *value* or *resolution rule* changed | v009, v011, v012, v013 |
| template text only | v005, v006, v007 |
| runtime / asset restructuring | v010, v014, v015 |

**7 of 15** (v002, v003, v004, v007, v008, v009, v011) exist because resolved
state is *stored* rather than computed. The other 8 would ship regardless:
v005, v006, v010, v014 are template-text or data-layout changes, and v012,
v013, v015 are **report-only** — they add no key and rewrite nothing, because a
resolution change has no old-default sentinel to match on. That is the tell:
the file records state that could be recomputed.

### 1.2 Project `templates/` is a copy that must be *consented* to update

`translate.py:370` copies every shipped template into the project at `init`.
`migrations/common.py:48` `sync_templates()` then refuses to overwrite a
differing file without consent (`--force`, or an interactive confirm). So once
a project exists, **a prompt improvement can never reach it silently** — every
prompt-text change needs a numbered step whose only job is to run the sync.
`v007`'s DESCRIPTION is the clearest case: it ships two new templates *and*
"refresh `tn_generate.md`", the refresh being required precisely because every
pre-v007 copy reads as drifted.

The runtime already resolves correctly — `lib/pipeline.py:392`
`_load_template()` falls back to `_SKILL_TEMPLATES` when the project file is
**missing**. The missing piece is only the *unmodified-but-present* case.

### 1.3 `DEFAULT_BASE_URL` is one machine's private LAN address

`lib/config.py:37`: `DEFAULT_BASE_URL = "http://100.85.218.125:8888/v1"`. It is
the fallback in all 7 `PROVIDER_DEFAULTS` blocks (`config.py:44-52`). A config
that omits `base_url` silently inherits an unreachable endpoint and fails at
the first call.

---

## 2. Target architecture

### 2.1 Config layering (Docker-style)

```
config.DEFAULTS / PROVIDER_DEFAULTS      (layer 0 — the image)
        ↓ overridden by
<skill>/config.local.json                 (layer 1 — machine-local, gitignored)
        ↓ overridden by
<project>/config.json                     (layer 2 — authored by the user)
```

`load_config()` composes all three at read time and returns the merged view.
Layer 2 is written with **authored keys only** — `init` stops calling
`cfg.update(config.DEFAULTS)` (`translate.py:298`) and stops calling
`apply_overlay` (`translate.py:316`), because under read-time layering that call
*is* the materialization the goal forbids, and it would make a later edit to
`config.local.json` lose to a stale project value. `cmd_sync_config`
(`translate.py:519`) loses its `apply_overlay` for the same reason and becomes
report-only.

`merge_overlay()` (`config.py:292`) is already the layer-1→layer-2 compositor
and its provider-array rules are exactly right (a job given as an **array**
replaces wholesale; a single-block **object** merges into every block so a
partial overlay cannot un-point a working endpoint). It is generalized from
"project ← overlay" to "layer N ← layer N-1" and applied once per pair.

**Three-layer composition is verified, not assumed.** Running DEFAULTS →
local `[A,B]` → project `{model}` yields `[{A, model}, {B, model}]`: the
single-block rule spreads across both blocks by design. This is correct, and
D1 documents the result rather than "fixing" it.

Two modules read **raw** config today and would silently ignore layer 1
(`logger._resolve_flags` at `logger.py:127`, `vcs._enabled` at `vcs.py:85`);
both move onto the layered loader in D1.

**`base_url` becomes required.** Each provider block must resolve a non-empty
`base_url` or `load_config()` raises with the offending job named. The private
LAN default is deleted — which makes `base_url` the one field the v009 sentinel
cannot safely strip, hence D1.4's targeted rule for it.

### 2.2 Template layering

```
assets/templates/<name>.md                (layer 0 — the image)
        ↓ overridden by
<project>/templates/<name>.md             (layer 1 — authored by the user)
```

`init` stops copying (`translate.py:369-375`). A project template file wins
**unless** it is byte-identical to some *known-shipped* revision of that
template — in which case it is an unmodified leftover and falls through to the
skill's copy, so shipped prompt improvements reach it automatically.

Known-shipped revisions are recorded in `assets/templates/.history.json`,
`{ "<name>.md": ["<sha256>", ...] }`, **seeded from git history** and appended
to when a maintainer authors a prompt change. The file is data, not state: it
lives in the skill, so a project carries no bookkeeping. A project file whose
hash is absent from the list is a user customization and always wins. The
append has no runtime component — `sync_templates` (`common.py:48`) is
per-project and never touches this file — so it is a maintainer authoring step,
recorded by the migration rule D2 pins (audit §A5, §A8).

### 2.3 Per-job consensus templates

Today there is one arbitration prompt: `consensus.py:48`
`_CONSENSUS_TEMPLATE = "consensus.md"`, loaded at `consensus.py:215`. The
arbitrator is already resolved **per job** for its *provider*
(`config.consensus_provider(cfg, job)`, `config.py:440`), but its *prompt* is
not — every job merges through the same wording.

v2 ships one template per job in `config.CONSENSUS_JOBS` (`config.py:9`, 6
entries — verified) — `consensus_translator.md`, `consensus_glossary.md`,
`consensus_reviewer.md`, `consensus_annotator.md`, `consensus_recap.md`,
`consensus_profile.md` — each with the same two placeholders
(`{{task_prompt}}`, `{{candidates_section}}`, both verified present in the
current `consensus.md`) so `fill()` stays unchanged. Resolution at
`consensus.py:215` becomes: `consensus_<job>.md` → fall back to `consensus.md`.

A job with a **single** provider block returns at `consensus.py:158-162`
before any template is loaded, so it is untouched — the per-job template is
resolved only by a job that actually fans out. The general template stays
shipped as the fallback, so a project that overrides only `consensus.md` keeps
working.

---

## 3. Phases

Each phase is implemented once, in the checkout, gated live, then adversarially
audited (P3, one round per phase).

### D1 — Config overlay layering

Five sub-tasks. **1, 3 and 4 are blocking**: each is a runtime failure on every
existing project if skipped (audit §A1, §A2, §A4). Six in total — sub-task 6
keeps four shipped migrations honest.

1. **`lib/config.py` layering.** Compose `DEFAULTS` → `config.local.json` →
   project config at read time. Generalize `merge_overlay` (`config.py:292`) to
   layer N onto N−1 and apply it per pair. Its provider rules already compose
   correctly across three layers — verified by running them: DEFAULTS → local
   `[A,B]` → project `{model}` yields `[{A, model}, {B, model}]`, the
   single-block rule spreading across both blocks by design. Document that
   result rather than "fixing" it.
2. **`base_url` required.** Delete `DEFAULT_BASE_URL` (`config.py:37`, the
   private LAN constant) and the `base_url` key from all 7 `PROVIDER_DEFAULTS`
   blocks; validate a non-empty `base_url` per resolved block at load, naming
   the job. **Test impact measured:** 4 suites reference the LAN literal
   (`test_log_dashboard.py:1335-1356`) and ~5 assert `PROVIDER_DEFAULTS`
   supplies `base_url` for sparse authored blocks (`test_provider_arrays.py`,
   `test_sync_config.py`, `test_consensus.py`).
3. **`init` must not crash.** Removing `cfg.update(config.DEFAULTS)`
   (`translate.py:298`) leaves the local `cfg` without `seed_min_count`, and
   `config.get_number` (`config.py:541`) has **no** DEFAULTS fallback and
   raises. `cmd_init` reads the local dict at `translate.py:402`. Fix by
   re-loading through `_load_config(project_dir)` after `save_config`, or by
   the `cfg.get(key, DEFAULTS[key])` pattern already used at
   `translate.py:418-419`.
4. **`v016` — the `base_url` sentinel.** For a project block whose `base_url`
   byte-equals the **retired** LAN constant, strip it and report
   `[warn] providers.<job>.base_url was the retired default <ip>; set it in
   config.local.json or the project`. Do **not** strip any other `base_url`: the
   v009 sentinel is safe for a value that still resolves through layer 0, and
   `base_url` is the one field for which layer 0 no longer has a value. Other
   `DEFAULTS` keys are stripped on value-identity per the v009 precedent, with
   every stripped key reported.
5. **Layer the two raw readers.** `logger._resolve_flags` (`logger.py:127` →
   `_raw_config`) and `vcs._enabled` (`vcs.py:85`) read raw `config.json` and
   would ignore layer 1 for `log_*` and `git_commits`. Route both through the
   layered loader, composing **once per invocation** to preserve the logger's
   documented no-per-event cost.
6. **Keep v002/v004/v007/v008 honest.** They call `common.standard_step`
   (`v002.py:29`, `v004.py:30`, `v007.py:46`, `v008.py:48`), and their
   `DESCRIPTION`s claim they materialize (`v007`: "materialize the `recap`
   provider job"). Switching them to call `materialize_config` **directly** —
   as `v001`/`v003`/`v011` already do — lets `standard_step` drop it without
   turning four shipped descriptions into lies for any project below v016.

**Removed in D1:** the `apply_overlay` calls at `translate.py:316` (init) and
`:519` (`cmd_sync-config`). Under read-time layering these *are* the
materialization the goal forbids, and they make later edits to
`config.local.json` lose to a stale project value. `sync-config` survives,
redefined as **report-only** — "here is what your layers resolve to" — keeping
its exit-code contract for a malformed overlay.

**Doc mirrors:** `references/file-formats.md` (layering table, deleted default,
required-key validation + exit code, v016), `SKILL.md` (`sync-config`
semantics, provider inheritance), `README.md`.

**Gate:** suite green, plus a live two-project check — one project with a
hand-edited threshold keeps it; a project whose `base_url` was the retired LAN
constant reports the warning and names the job.

### D2 — Template overlay layering + per-job consensus templates

- `lib/pipeline.py:_load_template`: apply the shipped-hash fall-through. All 11
  call sites are pure reads (verified — no write-then-read caller exists), so
  the change is mechanically safe.
- `translate.py:init`: stop copying templates (`translate.py:369-375`); report
  that the skill's templates are the base layer.
- `assets/templates/.history.json`: `{ "<name>.md": ["<sha256>", ...] }`.
  **Seed it with every shipped revision recoverable from git history**, not
  only the current one — `git log --follow` returns 2 distinct blob hashes for
  `translation.md` alone (`a6e2040b1c5e`, `d666c437a1c2`). Seeding only current
  hashes classifies every pre-D2 project's identical copy as a customization and
  orphans them permanently (audit §A5). Safe against `.gitignore` (no matching
  pattern) and against both `*.md` globs (`translate.py:370`,
  `common.py:64`).
- `assets/templates/consensus_<job>.md` × 6, sharing the two placeholders
  `{{task_prompt}}` / `{{candidates_section}}` verified present in the current
  `consensus.md`. `consensus.py:215` resolves `consensus_<job>.md` → `consensus.md`.
- `migrations/v017.py`: delete project template files whose hash is in
  `.history.json`; keep every customized one; report each.
- **Pin the `.history.json` appender.** `sync_templates` (`common.py:48`) is
  per-project and never touches the skill's file. The append happens when a
  maintainer **authors** a prompt change, and the maintainer-only AGENTS.md
  rule below keeps a migration for any history-file change so the append is
  recorded. Correctness does not depend on the list growing — the fall-through
  resolves to the *current* shipped file — but a stale classification would
  silently strand a project's templates (audit §A8).

**`AGENTS.md` — exact replacement wording for the migration rule.** The rule's
template half is replaced with:

> A change to a **project data shape** (a file's naming scheme, a file the
> project itself owns, like `chapters.json` or `story_state.json`) requires a
> new `scripts/migrations/vNNN.py`. A change to a **shipped default** in
> `DEFAULTS` / `PROVIDER_DEFAULTS`, or to a **shipped prompt template** in
> `assets/templates/`, does **not**: both resolve through the overlay chain
> (§ file-formats.md "Overlay layers"), so the change reaches every project on
> its next run. One exception: editing `assets/templates/.history.json` itself
> requires a migration, because its correctness depends on the recorded
> revisions, not on the current text.

The config half is narrowed by the same paragraph. **D2's audit checks the
committed `AGENTS.md` against this wording, not against a paraphrase** (audit
§A7).

**Doc mirrors:** `SKILL.md` (template resolution, per-job consensus templates,
`init` no longer copying), `references/file-formats.md` (overlay table,
`.history.json`, v017), `references/maintenance.md` (only if a maintenance
subcommand gains a template).

**Gate:** suite green, plus a live check that a customized project template
still wins, and that a shipped prompt edit reaches an unmodified project.

### D3 — Whole-book `verify` (split into D3a and D3b)

The original one-liner — *"scan `translated/` against the final glossary"* —
understated the work. Resolved with the maintainer on 2026-10-11 before
implementation; the decisions are recorded here rather than left to the
implementer.

**Definition of drift (maintainer):** everything that would **infect future
translations with a contaminated glossary item** — a term that would force a
translation to match the glossary entry where doing so would mistranslate,
because the word means different things in different contexts.

**Shape:** two stages. Stage 1 is deterministic and free — match a chapter's
**source** against the glossary, then check how the **translation** rendered
those terms (the BALANCE shape). Stage 2 spends LLM calls **only on the chapters
stage 1 flagged**, and asks specifically the semantic question: does forcing this
glossary term here produce a mistranslation?

**Output:** JSONL, one line per chapter, shaped for later visualization.
Visualization is out of scope.

**Advisory, but actionable:** verify flags chapters into the existing human
review surface. It never rewrites translated text.

**Split rationale:** D3a is the capability and the command — standalone,
auditable, no automation risk. D3b is the automation, which adds default-on LLM
spend and touches FAITH. Splitting keeps the first shippable and means the
second can fail loudly without blocking anything.

#### D3a — `lib/verify.py` + `translate verify`

Two-stage capability, exposed as one command. **No auto-trigger, no FAITH hook.**

- `lib/verify.py`: stage 1 deterministic scan; stage 2 LLM on flagged chapters.
- `translate verify`: runs both, writes the JSONL, flags chapters for human
  review through the existing surface. Never mutates translated text.

**Decisions taken with the maintainer on 2026-10-11 (the implementer stopped and
asked rather than guessing — that was correct):**

1. **Stage 2 gets its own `verify` provider job and its own
   `consensus_verify.md`.** Reuse was ruled out by evidence, not preference:
   `consensus_reviewer.md` instructs the arbitrator that *"pinned glossary
   renderings are project decisions and are **not** translation errors"*, and
   `consensus_glossary.md` that *"the pinned one wins"*. Reusing either would
   instruct the merge to discard the very verdict stage 2 asks for.
2. **That new job ships a `v018` migration — belt-and-braces.** Provider
   inheritance would resolve `providers.verify` at read time without touching a
   file, which reads as migration-free under the post-D1 rule; the maintainer
   chose the stricter reading, so the upgrade is recorded.
3. **Flagging flips chapter status, and ONLY on stage-2 confirmation.** A stage-1
   heuristic hit must never flip a chapter, because a status flip makes it
   eligible for `retry --failed`, i.e. it schedules a re-translation.
4. **JSONL location: `logs/verify.jsonl`**, `--out` to override, one line per
   chapter **examined** (clean chapters included with empty findings) so a later
   visualization can be built from it.

A coupling to watch: if `verify` joins `CONSENSUS_JOBS`, then `consensus_verify.md`
must ship **and** be recorded in `.history.json` — `tests/test_template_assets.py`
asserts every shipped template's current hash is present.

#### D3b — auto-trigger and the FAITH investigation

- **Auto-trigger, default ON:** after a glossary-changing operation —
  `review --fix`, and the glossary retire / merge / replace paths — verify runs
  over the chapters the change could have affected.
- **FAITH investigation:** when the FAITH gate rejects a chapter for a reason
  that looks like glossary over-binding, run the stage-2 investigation on it.

Both change behaviour for existing pipelines, which is why they land after D3a
has proven the capability.

### D4 — Dashboard data/render split

`lib/logdashboard.py` is 2,200 lines and the largest file in the repo. Measured
precisely: 796 lines data collection, 563 HTML fragment builders (not one
contiguous block — ~17 builder functions), 226 CSS + 402 JS as verbatim string
literals. It emits a 3.5 MB `report.html`.

Split: `collect()` writes `logs/report.json` (the data contract, diffable,
testable); `render()` reads that JSON and writes HTML from shipped
`assets/dashboard/*` template files, so the linter can see them.
`tests/test_log_dashboard.py` (1,393 lines, 67.9 KB — the largest test file,
mostly markup assertions) retargets the JSON contract.

**Doc mirrors:** `references/file-formats.md` (`logs/report.json` schema),
`README.md` + `references/maintenance.md` (the logs surface).

### D5 — Docs: generate the derivable, fix the drift

- Generate the CLI surface table from the argparse tree into
  `references/cli.md`, so subcommands/exit codes cannot drift again. Live drift
  at HEAD: `translate.py:8-9` lists 17 subcommands and omits `logs`
  (registered at `:1685`), and the same docstring omits exit 3 — which **is**
  emitted, at `translate.py:2084-2095` (`return 3` on `client.LLMFatal`).
- Correct the sequential-execution rationale in `SKILL.md` — the current
  reason ("the glossary grows as you go") is false, since `_gate_corpus()`
  already counts novel-wide; the real reason is deterministic, resumable
  batches.
- **Report fan-out cost as a factor only.** A clean chapter costs 6–7 provider
  calls, each ×N models + 1 arbitrator under a multi-block job. Per
  `AGENTS.md`'s provider-routing rule this is reported as a number, with no
  recommendation to narrow a job's provider array and no model demoted.

**Doc mirrors:** `references/cli.md` (new, generated), `SKILL.md` (rationale),
`README.md` (CLI list).

---

## 4. Migration numbering

Current head is `v015`, read from `scripts/migrations/` at implementation
time rather than from this document. D1 ships `v016`, D2 ships `v017`.

---

## 5. Risks

Audited 2026-10-11 (`AUDIT-2026-10-11-v2-overlay.md`); three rows added after.

| Risk | Mitigation |
|---|---|
| v016 stripping a default the user had *deliberately* set to the same value | Value-identity, not intent. The stripped file still resolves to the same value through layer 0 — **except** `base_url`, whose layer-0 value D1 deletes, so it gets its own targeted rule (D1.4). Every stripped key is reported. |
| A project template customized against an *old* prompt loses its anchor when the prompt changes | `.history.json` is append-only and seeded from git history, so a customization stays a customization forever and keeps winning. |
| Dropping `materialize_config` breaks `v002`/`v004`/`v007`/`v008`, whose `DESCRIPTION`s claim they materialize | Those four call `common.standard_step` (`v002.py:29`, `v004.py:30`, `v007.py:46`, `v008.py:48`). D1 must switch them to call `materialize_config` **directly** — as `v001`/`v003`/`v011` already do — so their advertised behavior survives for projects below v016. |
| A below-v016 project runs v006–v015 (which materialize) then v016 (which strips) | Correct, just wasteful; net result is the v2 shape. v016's report lines make the round trip visible. |
| **A project that never runs `migrate` keeps the old model** | Layering is backward-compatible on read (an unmigrated project simply carries extra keys that resolve to the same values), so nothing breaks — but it carries dead weight. `sync-config`'s new report-only mode surfaces the drift. |
| **`.history.json` goes stale because no appender exists** | The AGENTS.md replacement keeps a migration for history-file edits, so an append is recorded; and the fall-through resolves to the *current* shipped file regardless, so a stale list degrades classification, never correctness. |
| **Two raw config readers bypass layer 1** | D1.5 — `logger._resolve_flags` and `vcs._enabled` move onto the layered loader, composing once per invocation. |
| D4 is the largest single change and touches the most tests | Sequenced last; its own round can fail loudly without blocking D1–D3. |
| `AGENTS.md` change (D2) alters a repo-wide convention | Exact replacement wording pinned in D2; audited against that wording, not a paraphrase. |
| **Plan-mirroring tests survive the loop** (known, per `workflows/verification-loop.md` §5) | The goal is the mitigation. D1's live checks assert on **two real projects**, not on the layering helper's return value. |

---

## 6. Explicitly out of scope

- Provider routing quality/cost changes — `AGENTS.md` forbids demoting a model
  or trimming `max_tokens` without being asked. D5 reports fan-out cost as a
  number only.
- Parallelizing `translate`. Sequential is correct for resumability; only its
  *stated reason* is wrong.
- Any change to the 8-stage pipeline's gates, thresholds, or ordering.
- **`style.md` layering** (audit §A11). `init` copies a preset into
  `style.md` (`translate.py:271-273`) and the project hand-edits it, so shipped
  preset improvements never reach an existing project — the same defect class
  as templates. It is excluded because it is a *chosen project data shape*
  (the preset name is recorded in `novel_info.json`), not a template override:
  there is no honest signal distinguishing "the user edited this guide" from
  "this is the unmodified preset". Shipping it half-layered would be worse than
  leaving it explicit. The goal's phrase "every prompt template" is narrowed to
  templates resolved through `_load_template`.