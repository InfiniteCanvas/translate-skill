# PLAN — context blocks for `translate verify` stage 2

**Date:** 2026-10-12
**Status:** proposal, not yet implemented. No code has been changed.
**Revision:** **rev 1** — rewritten after the P1 static round
(`AUDIT-2026-10-12-verify-context-blocks.md`, 4 lenses). Rev 0 failed its own
central clause and is superseded, not amended. Every change below traces to a
finding; the disposition of all findings is §7.
**Author scope:** `D:\Repos\vibed\translate-skill` @ HEAD `c720c2e` (`main`).
**Baseline:** `uv run tests/run_all.py` → **54 passed, 0 failed (54 scripts,
2673 checks)**, verified live on `main`. The runner hides any ambient
`config.local.json`, so the gate is hermetic either way.
**Predecessor:** `PLAN-2026-10-11-v2-overlay.md`, landed at `c720c2e`.

---

## Pinned functional goal — round-invariant

> **For a term judged by `translate verify` stage 2, the evidence presented to
> the model is not repeated.** Where a term's hits fall close together, the
> current code emits several near-identical 5-line windows and the model reads
> the same sentence up to eight times. After this change, **each
> `(chapter, line_number)` pair is presented at most once across ALL calls made
> for that term**, every hit line falls inside some presented range, and the
> union of presented lines is exactly the union of that term's per-hit
> ±`CONTEXT_WINDOW` windows — no line dropped, none added.
>
> **No batch exceeds `BATCH_CHARS` (128,000) rendered characters.** The cap bounds
> the **rendered evidence** — what the model actually reads — and is measured
> against that form, not a cheaper proxy that under-counts. It does not bound
> the whole prompt: the rubric, the term block and the instruction preamble are
> excluded, because a cap that counts text the model never sees as evidence
> measures the wrong quantity.
>
> The number is a **maintainer ruling, not a derivation**: every provider in use
> has a context window of at least 256k, so a 32k ceiling bound nothing real and
> only truncated batches the provider would have handled comfortably. Measured on
> the real run (§2 V2), rendered p95 was 10,674 with a single block reaching
> 34,873 — already past a 32k cap, so 128k leaves headroom for books far longer
> than the example project while keeping the cap honest.
>
> Because the cap is no longer derived from `MAX_CONTEXTS_PER_CALL = 8`, the
> invariant that keeps packing terminating is stated explicitly instead: **a
> batch always carries at least one block.** A block that alone exceeds the cap
> still ships as a one-block batch rather than looping forever.
>
> **The verdict contract is unchanged:** one verdict per `(term, chapter)`, and
> the fields a verdict carries (`source_count`, `rendered_count`, `floor`) are
> unchanged, because all three are per-chapter fields that block formation
> cannot alter.

**Why this is an evidence-quality change and not a cost change.** The value is
that the model spends its attention on distinct lines instead of re-reading the
same sentence. Cost is a secondary consequence and is reported as a factor, per
`AGENTS.md` — it is never the reason a provider setup is changed.

**What this plan does NOT claim.** It does not claim coalescing improves verdicts.
That is unmeasured and stays marked outstanding (§2 V2). What it claims is
falsifiable and testable: no line is presented twice.

Every audit tests against the statement above and `AGENTS.md`, never against this
plan's internal consistency.

---

## 1. What is actually wrong today

### 1.1 Repeated evidence

`_context` (`verify.py:157-178`) emits the ±2 window around **one** hit line,
source and translation, each line truncated to `CONTEXT_CHARS = 400`. When a
term's hits fall within a few lines of each other, those windows overlap heavily
and the same sentence is paid for, and read, repeatedly.

A structural probe measured the deduplication ratio against hit spacing
(`%TEMP%\probe_overlap.py`, run live):

```
20 hits, 1 line(s) apart    raw 100  union 24   4.17x
20 hits, 2 line(s) apart    raw 100  union 43   2.33x
20 hits, 3 line(s) apart    raw 100  union 62   1.61x
20 hits, 5 lines apart      raw 100  union 100  1.00x
20 hits, scattered 0-300    raw 100  union 100  1.00x
```

**Two corrections to figures this plan inherits.** The earlier note claimed
"roughly 6x"; the structural ceiling for a ±2 window is **4.17x**, and it is
geometry-dependent — in a chapter exactly the hit-span long the ratio is 5.00x.
More importantly **a term whose hits are all ≥5 lines apart gains nothing at
all**, so the book-level figure is unknown until measured (§V2). The 4.17x is a
ceiling for tightly-clustered terms, not an average, and no average is claimed
here.

### 1.1a MEASURED baseline — `example/`, 2026-10-14

The bound above was superseded by a real measurement taken on the repository's
fully translated example project **before** any code changed, using the shipped
`_hit_lines` unmodified so the locator is the production one:

| | today | after chapter-scoped coalescing |
|---|---|---|
| blocks | **8,414 contexts** | **3,397** (−59.6%) |
| line payload | 42,070 | 28,239 (−32.9%, **1.49x**) |

154 glossary entries, 73 source chapters, all entries matching at least one
line. **69.5%** of consecutive hits (4,666 / 6,714) fall within coalescing range
(≤4 lines apart); the gap histogram is the mixed picture §1.1 predicted —
1,788 gaps at 1 line, 1,471 at 2, 859 at 3, and a long tail beyond 4 where
nothing merges.

**The measured figure is 1.49x, not the 6x the implementing note claimed and not
the 4.17x ceiling.** A real book mixes clustered and scattered terms, so it lands
between the two bounds. Rev 1 refused the 6x figure and committed to measuring
instead; this is that measurement, and it landed inside the range the honest
bound predicted.

**The evidence-quality argument survives the correction better than the cost
one.** The model is currently handed ~42,000 lines of evidence for this glossary,
of which ~13,800 are repeats of a line already present in the same call. After
V1 it receives ~28,000 lines, each appearing at most once.

**Method note, recorded because it nearly produced a wrong headline.** The first
measurement run reported **12.82x**, merging a term's hits across chapter
boundaries — line indices restart at 0 per chapter, so hits from unrelated
chapters coalesced into meaningless blocks. Blocks are chapter-scoped
(`_context` carries exactly one `chapter`); re-run per chapter the figure is
1.49x. A single unit error turned a 33% saving into a fabricated 93% one, and
the output looked entirely plausible. Same class as the no-op shipped behind a
green gate: a confident number that is wrong.

### 1.2 Two batching loops, not one

`MAX_CONTEXTS_PER_CALL` appears at **two** independent sites:

- `verify_project` — `verify.py:421-431`
- `investigate_faith` — `verify.py:537-543`

Both carry the same `range/step/call/_merge` shape. Rev 0 named only the
constant and neither loop; V1 changes **both**.

### 1.3 The cap does not currently bind

Because a mid-chapter context is exactly 5 lines, count-batching and
payload-batching coincide today — so `8` is a count wearing a budget's clothes.
At chapter edges a context is **3 or 4 lines**, because `_context` clamps with
`max(0, index - CONTEXT_WINDOW)` and `min(len - 1, index + CONTEXT_WINDOW)`
(`verify.py:166-167`), so the coincidence is approximate, not exact.

This is why coalescing and the cap are one atomic change. Block formation turns
a fixed-size unit into a variable-size block; **landing coalescing under a
count-cap would ship unbounded prompts**, because a 200-line block would count
as "context #1 of 8". Rev 0 asserted this safety case on a false premise
("every context is exactly 5 lines"); the property that actually holds is the
explicit `BATCH_CHARS` ceiling.

---

## 2. Phases

### V0 — the line-parity guard rejects every chapter (added 2026-10-14)

**Found while preparing V2's measurement, not by any audit.** `verify_project`
reads the source through `project.read_chapter` — which strips YAML frontmatter
(`project.py:181-191`) — but reads the **translation raw** (`verify.py:494`),
and `_chapter_lines` strips only blank lines. `pipeline.py:1689` writes
frontmatter into every translated chapter via `assemble.assemble(out_path, fm, …)`,
so every chapter this pipeline produces carries 8 frontmatter lines plus one
blank, and the parity guard at `verify.py:502` rejects it.

Measured on `example/`, all 73 chapters: **0 pass parity**, every one short by
exactly 9 lines. With frontmatter stripped, 73/73 pass, 39 chapters are flagged,
55 under-used rows form, V1 produces 72 blocks, and stage 2 would make **39
provider calls** instead of zero.

**Consequence: `translate verify` has never checked a chapter on any project.**
D3a's stage-2 audit, D3b's auto-verify trigger, and V1's coalescing are all
unobservable until this lands.

V0 fixes the read and adds a regression test carrying frontmatter that fails
today. A genuine parity mismatch must still be rejected — the guard is right,
its input was wrong. No other behaviour changes.

### V1 — Context blocks + explicit byte cap (atomic)

**V1.1 — Block formation, before batching.**
For each `(term, chapter)`, sort hit lines and merge windows whose
`[low, high]` ranges **overlap or touch** into one block spanning
`min(low)..max(high)`.

Two properties the implementation must hold:

- **Union-preserving.** A merge may never widen a block past the union of its
  constituents, and may never join ranges that do not overlap or touch. Ranges
  with a gap between them stay separate blocks. Rationale belongs in the
  `_blocks` docstring, not a `#` comment (`AGENTS.md` comment ban).
- **Ordered.** Blocks within a term are emitted in non-decreasing
  `(chapter, low_index)` order. Ordering drives model attention and is not
  invariant under the stated risks, so it is specified and tested rather than
  left to whatever the sort happens to produce.

Formation happens **before** the batch loop. Slicing hits into batches and
coalescing within each batch would produce blocks that overlap *across* batches
— the model re-reads lines and the saving is lost while every per-prompt test
passes. This is the specific defect V1.6 exists to catch.

**V1.2 — Explicit cap.**
`MAX_CONTEXTS_PER_CALL` is replaced by `BATCH_CHARS = 128_000`, a **maintainer
ruling recorded in the pinned goal** rather than a number computed from this
module's constants: every provider in use has a context window of at least
256k, so the 32,000 the old count cap implied (`8 × 5 × 2 × CONTEXT_CHARS`)
bound nothing real. Its docstring states that basis, and that the config
carries **no input-context key** (`max_tokens` and `max_tokens_limit` are
output caps), so a per-project derivation is not available without inventing a
key.

Because the cap is no longer derived from the deleted count cap, the invariant
that keeps packing terminating is stated explicitly: a batch always carries at
least one block, so an oversized single block ships as a one-block batch
instead of looping.

**What `BATCH_CHARS` measures — pinned, because the goal sentence alone is
ambiguous.** It bounds the batch's **rendered evidence**: exactly what
`_render_blocks` emits for that batch — each block's header line, its
`[N] ` prefix, its occurrence counts, and every source and translation line,
indentation included. It does **not** bound the whole rendered *prompt*: the
rubric, the term block and the language names are constant per call and are not
counted, because the goal's own arithmetic (`8 × 5 × 2 × 400`) counts lines and
could not derive a cap that also counted prose.

**Amended 2026-10-15 by maintainer ruling, after measurement.** The first
implementation counted a *proxy* — chapter name, line range, and the raw line
strings — while `_render_blocks` emitted more than that. Measured on the real
book, the proxy under-counted by **14.8%**, and the largest batch rendered at
**34,873** — already past the 32,000 cap that was in force, i.e. the cap was
binding at the tail rather than decorative. The cap now measures the rendered
form directly, so "no batch's evidence exceeds `BATCH_CHARS`" is literally true
rather than true-of-a-proxy. That finding is why the cap is specified against
the rendered form at all, and it stands independently of the number: a cap that
measures a proxy measures nothing the model reads.

A batch is packed with blocks until adding the next would exceed `BATCH_CHARS`;
a single block larger than the cap ships alone. Both loops from §1.2 use it.

**V1.3 — The prompt describes what it receives.**
`glossary_verify.md:7` currently reads "This batch holds `{{context_count}}`
context(s)". Under blocks that is a block count, while the rubric's rules 3 and 4
reason about **occurrences**. The template reports **both**:

```
This batch holds {{block_count}} block(s) covering {{hit_count}} occurrence(s)
of that term.
```

so the model's account of its evidence stays honest. Both placeholders are
filled by `_prompt`; `pipeline.fill` raises on an unfilled placeholder, so a
missing one fails loudly rather than shipping an empty slot.

`glossary_verify.md:21` ("a window of lines around one place the term occurs")
becomes "a span of lines around one or more places the term occurs", and line 30's
comparison instruction is **preserved verbatim** — comparing usages against each
other is the purpose of the stage, and deduplication serves it.

`consensus_verify.md:22-23` carries the same context vocabulary and is **in
scope** for the parallel edit: the arbitrator reads its own rubric on every call.

**V1.4 — Rubric invariants, with replacement wording pinned.**
All **six** numbered rules survive. This plan pins the literal replacement text
so the implementer cannot paraphrase and the audit compares against pinned
wording rather than an implementer's reading (the A7 lesson from the previous
campaign):

| Rule | Status |
|---|---|
| 1 — read the source and translation lines, they are the evidence | preserved verbatim |
| 2 — compare the contexts against each other | preserved verbatim |
| 3 — the counts are a question-generator, not a charge | preserved verbatim |
| 4 — an unused term is not a drifted term | preserved verbatim |
| 5 — do not flag style | preserved verbatim |
| 6 — when in doubt, answer false | preserved verbatim |

The only edits are the evidence-unit vocabulary in lines 7 and 21 and the new
`{{block_count}}`/`{{hit_count}}` pair. **No rule is reworded, reordered, or
dropped.**

**V1.5 — Trace attribution, decided rather than inherited.**
`_verdicts` passes `chapter=Path(contexts[0]["chapter"]).stem` (`verify.py:263`)
for tier-2 bucketing. Under blocks, `contexts[0]` is the first block in the
batch's ordering, which is deterministic under V1.1's order but is a different
value from today's "first hit's chapter". V1 makes this explicit: attribution is
**the first chapter in the batch's block order**, stated in `_verdicts`'
docstring, and a test pins it. This is an acknowledged behaviour change in
`logs/verify.jsonl`, not a silent one.

**V1.6 — Tests that can fail.**
Three tests, each **mutation-proven** — the implementation is broken in a way
that must turn each test red before the phase can land:

1. **Cross-batch union.** Across ALL calls emitted for one term, the set of
   `(chapter, line_number)` pairs presented equals the union of that term's
   per-hit ±2 windows, **and** each pair appears in at most one call's prompt.
   Stated per-prompt, this test passes an implementation that duplicates across
   batches; stated globally it cannot.
2. **Ordering.** Within a call, blocks appear in non-decreasing
   `(chapter, low_index)` order.
3. **Trace attribution.** A batch's `chapter=` is the first chapter in its block
   order.

The "line" unit is `(chapter, 1-based line_number)`; truncation at
`CONTEXT_CHARS` is identical wherever a line appears, so the unit does not depend
on rendering.

`case_contexts_are_split_into_follow_up_calls` (`test_verify.py:392-421`)
**is rewritten, not supplemented** — it imports `verify.MAX_CONTEXTS_PER_CALL`
directly at `:399` and asserts a count-based split at `:405-407`, and both die
with the constant. Its "across N call(s)" assertion at `:415` must keep holding.

### V2 — Measurement, and the doc mirrors

**V2.1 — Measure.** §1.1a already recorded the **before** distribution on a real
project. V2 takes the **after**: block count, lines per block, payload per batch,
calls per term, and the P95 payload. Report both sides side by side, so the
shipped cap is chosen against an observed delta rather than a projected one.

**V2.2 — Cap confirmed against the measured distribution.** Recorded 2026-10-15
from a live run on a scratch copy of `example/` (73 chapters, 39 flagged by
stage 1, 55 verdicts, **24 batches**, 72 blocks, 23 terms with evidence):

| percentile | proxy cost (old) | rendered (new, authoritative) |
|---|---|---|
| median | 1,200 | 1,399 |
| p90 | 7,355 | 8,291 |
| **p95** | **9,604** | **10,674** |
| max | 31,376 | 34,873 |

These numbers were measured **with 32,000 in force**, and they stand as
measured. They are the evidence for two separate rulings, which are easier to
read together than separately:

- **The cap measures the wrong quantity** (the 14.8% proxy finding above). The
  rendered column is authoritative; the proxy column is kept only to show the
  gap the first implementation shipped with.
- **The cap was the wrong size.** Rendered p95 is 10,674 — about **12× headroom**
  under 128,000 — and the largest single block reached 34,873, already past a
  32,000 ceiling. The cap was binding only in its extreme tail, against a
  provider that carries at least 256k, so it truncated batches the model would
  have read comfortably. `BATCH_CHARS` is therefore **128,000** (pinned goal).

The earlier rule — *the cap is set so no more than 5% of batches are split by
it* — no longer chooses the number and is retired rather than reworded: at
128,000 the expected split count on this distribution is zero, which is the
headroom the ruling asks for, not a statistic the cap is tuned to. A real book
whose chapters run far longer than the example's still splits, and that branch
is covered by tests sized against the shipped constant.

This measurement is also what exposed the proxy/rendered gap above and forced
the maintainer's ruling, so it did the job V2.2 exists for: a number chosen
against an observed delta rather than a projection.

**V2.3 — Rubric quality check.** Run the sentinel proof the previous campaign
established: capture a real prompt and assert the term, chapter and both language
names are present and **zero placeholders are unfilled**. D3a's stage 2 shipped
with zero placeholders and survived a green gate; this check is mandatory.

**V2.4 — Doc mirrors, enumerated.** Every site below changes, and V2 greps for
any it missed before landing:

| File | Lines | Change |
|---|---|---|
| `references/file-formats.md` | 1901, 1910, 2127 | window/batching prose; payload schema row gains `block_count`/`hit_count` |
| `SKILL.md` | 801, 803 | verify stage-2 description and batching rule |
| `README.md` | 502, 506 | same |
| `references/maintenance.md` | 337-340 | same |
| `assets/templates/glossary_verify.md` | 7, 21 | evidence-unit wording (V1.3) |
| `assets/templates/consensus_verify.md` | 22-23 | parallel vocabulary |
| `scripts/lib/verify.py` | 44-47, 88-92 | module docstring and constants |
| `tests/test_verify.py` | 399, 405-407, 415 | rewrite the split case |

Explicitly **not** touched, so the list is closed rather than silently skipped:
`references/cli.md` (generated from argparse; this change adds no flag and no
`DEFAULTS` key, so it does not drift), `config.local.EXAMPLES.md`,
`references/ingestion.md`, and **`references/file-formats.md:997-998`** — that
`±2` belongs to `review_notes`, a different constant in
`scripts/lib/review_notes.py`, and a grep for "±2" will hit it.

---

## 3. Migration numbering

Head is `v018`, read from `scripts/migrations/` at implementation time.

V1 edits `glossary_verify.md` and `consensus_verify.md`, both **released**
templates with entries in `assets/templates/.history.json`. Per `AGENTS.md`,
editing the history file requires a migration, so **V1 ships `v019`**.

Two corrections to rev 0's stated rationale, both from the P1 round:

- **`sync_templates()` does not write `.history.json`.** Per
  `migrations/common.py`, it copies a shipped template into a project. The
  history entry is a maintainer-side edit in the same commit. `v019` copies;
  the history append is separate.
- **The change reaches existing projects without any of that.** Rev 0 claimed
  v018-era projects would be stranded on the old rubric. That is wrong:
  `pipeline._load_template` (`pipeline.py:443`) resolves to the skill's copy when
  the project file is byte-identical to ANY shipped revision, and its docstring
  states the intent — "that second case is what lets a shipped prompt improvement
  reach an existing project". `v019` is belt-and-braces, not the delivery
  mechanism. **No forced overwrite is required or permitted.**

---

## 4. Risks

| Risk | Mitigation |
|---|---|
| **Block formation lands inside the batch loop**, producing blocks that overlap across batches — model re-reads lines, saving lost, every per-prompt test green | V1.1 specifies formation-before-batching; V1.6's test 1 is scoped across all calls for a term, not per prompt |
| **A batch exceeds the cap** | The cap bounds the **rendered** evidence, measured by the same renderer the prompt is built from, and packing terminates on the stated floor-of-one-block invariant: a batch always carries at least one block, so an oversized block ships alone instead of looping |
| **Trace attribution moves** (`contexts[0]`) | V1.5 decides it explicitly and pins it with a test; recorded as a behaviour change, not a surprise |
| **The prompt edit guts the rubric** | V1.4 pins all six rules verbatim and the replacement text; the audit reads the shipped file against pinned wording |
| **The prompt ships with an unfilled placeholder** — D3a's exact failure mode | `fill` raises; V2.3's sentinel proof is a gate step |
| **Blocks dilute attention**, making a large term harder to judge | The union invariant removes repetition rather than adding material. Whether verdicts change is **unmeasured** — see §5 |
| **The cap has headroom it does not need**, or is tighter than reality | V2.2 decides it from a pre-stated statistic, and records the outcome either way |

---

## 5. What stays unmeasured

Coalescing is proven to make evidence **less repeated**. It is **not** proven to
make verdicts **better**. The honest form of that claim is: the model reads each
line once instead of up to eight times, which is a precondition for spending
attention well — not a measurement that it does.

**A book-level A/B remains outstanding.** Running the same chapters before and
after, with a human read of contested lines, is the only thing that answers it,
and it needs a real project. V1 lands on mutation-proven structural proof and
marks the quality claim as unmeasured rather than asserting it.

---

## 6. Out of scope

- **`CONTEXT_WINDOW = 2` itself.** One variable per change: if blocks and a
  wider radius moved together, any verdict change would be unattributable. This
  is the knob most likely to affect judgment quality, and it is deliberately
  untouched so that the next measurement can isolate it.
- **`CONTEXT_CHARS = 400`.** Per-line truncation is independent of block width;
  changing it alongside would confound the same way. Its interaction with wide
  blocks is recorded in §V2.1's instrumentation.
- **What counts as drift, and the `(term, chapter)` verdict grain.**
- **Stage 1**, the deterministic under-use pre-filter.
- **Any provider-routing change.** No model demoted, no provider array narrowed,
  no `max_tokens` trimmed.

---

## 7. Disposition of the P1 round

Four lenses: goal-testability, goal-achievement, feasibility, convention-poisons.
Full text in `AUDIT-2026-12-12-verify-context-blocks.md`.

**Accepted and folded in:** the phantom provider-config derivation (three lenses,
independently); the second batching loop; the cross-batch scope of the union
test; "exactly once" not defining its line unit; block ordering unspecified;
`context_count` misrepresenting the evidence; the 4.17× ceiling being
geometry-dependent; `contexts[0]` trace attribution moving; four of six rubric
rules unprotected; `consensus_verify.md` forced in scope; the split-test rewrite;
`file-formats.md:997` belonging to `review_notes`; the `sync_templates` /
`.history.json` conflation; `cli.md` explicitly closed; the module docstring
going stale; rationale belonging in docstrings under the comment ban; and
mutation-proving being required.

**Refuted — the plan must NOT act on it.** The conventions lens reported as
blocking that `v019` would not reach existing projects, because v018-era copies
would match a shipped hash and keep the old rubric.
would match a shipped hash and keep the old rubric. That inverts
`pipeline._load_template` (`pipeline.py:443`), which resolves to the skill's copy
for exactly that case. Acting on it would have meant force-overwriting every
project's template — destructive, consent-gated, and aimed at a premise that is
backwards.

**Recorded deviations from rev 0:** the cost framing is replaced by an
evidence-quality framing (maintainer decision, 2026-10-12); `CONTEXT_WINDOW`
stays out of scope (maintainer decision); mutation-proven tests are required and
the book A/B is marked outstanding rather than blocking (maintainer decision).