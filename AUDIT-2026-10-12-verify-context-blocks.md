# AUDIT — `PLAN-2026-10-12-verify-context-blocks.md`

**Date:** 2026-10-12
**Round:** P1 static, four lenses in parallel — goal-testability,
goal-achievement, feasibility, convention-poisons.
**Subject:** rev 0 @ `PLAN-2026-10-12-verify-context-blocks.md`, repo `main` @ `c720c2e`.
**Verdict:** **REWRITE.** Three lenses independently found the plan's central
clause unimplementable, and the fourth found a test that cannot distinguish a
correct implementation from a broken one. Rev 0 is superseded, not amended.

---

## Why the round ran

The plan followed a landed campaign whose central capability — `translate verify`
stage 2 — shipped a **complete no-op** behind a green gate (a prompt with zero
`{{}}` placeholders, tests stubbing the provider call). The maintainer's stated
lever is judgment *quality*; this plan was drafted as a cost fix. Both facts
made it worth an adversarial pass before any code moved.

## The two structural failures

### 1. The budget clause had no mechanism

Rev 0's pinned goal read "No unmeasured magic constant decides how much evidence
a model sees", and V1.2 delivered it by deriving a budget "from the resolved
provider config — the model's context capacity and per-call headroom."

**No such key exists.** Three lenses confirmed independently:
`DEFAULTS` (`config.py:25-49`) and `PROVIDER_DEFAULTS` (`:61-70`) carry no
input-context key; `block_cap` (`:552-576`) reads only `max_tokens` and
`max_tokens_limit`, both **output** caps. So the implementer would land a new
hard-coded constant and the goal sentence would be false at the moment of
shipping.

Two related misses: `CONTEXT_CHARS = 400` (`verify.py:92`) is *already* an
unmeasured constant in the very module V1 edits, and the plan never mentioned
it; and expressing the budget in characters while the goal reasons about model
context in tokens introduces a chars↔tokens ratio the plan does not own.

**Resolution (rev 1):** the derivation claim is deleted rather than satisfied.
`BATCH_CHARS = 32_000` is the **explicit form of the ceiling
`MAX_CONTEXTS_PER_CALL = 8` already implied** —
8 contexts × 5 lines × 2 (source + translation) × 400 chars. No batch that
shipped before can exceed it, so none can get larger. The docstring states this
basis and states plainly that the config carries no input-window key, so no
per-project derivation exists without inventing one.

### 2. The union test could not fail

Rev 0's V1.6 specified "the emitted lines equal the union of the per-hit
windows, and that no line is emitted twice" — with no scope qualifier.

An implementer who coalesces **inside** the batch loop rather than before it
produces blocks overlapping *across* batches. For a term with 30 contiguous
hits, lines 7 and 8 appear in batch 1's block and again in batch 2's. The model
re-reads them, the expensive calls cost exactly what the plan promises to save,
and the per-prompt test passes throughout.

This is the D3a failure mode reproduced at the specification level: a green gate
over a feature that does not do what it claims. Rev 0's safety case for "coalesce
and the cap are atomic" also rested on a false premise ("every context is
exactly 5 lines") — false at chapter edges, where clamping yields 3 or 4
(`verify.py:166-167`).

**Resolution (rev 1):** V1.6's test is scoped **across all calls for a term**,
and block formation is specified as happening *before* the batch loop. Tests are
mutation-proven — each must be shown to go red against a deliberately broken
implementation.

## Findings accepted

| Finding | Lens | Resolution in rev 1 |
|---|---|---|
| Second batching loop `investigate_faith` (`verify.py:537-543`) omitted | feasibility | §1.2 names both; V1.2 applies the cap to both |
| "Exactly once" does not define its line unit under `CONTEXT_CHARS` truncation | testability | unit fixed as `(chapter, 1-based line_number)` |
| Block order unspecified, and order drives attention | testability | V1.1 mandates non-decreasing `(chapter, low_index)`; tested |
| `{{context_count}}` becomes a block count while rules 3-4 reason about occurrences | goal-achievement, feasibility, testability | V1.3 reports `{{block_count}}` **and** `{{hit_count}}` |
| V1.5 protected 4 of the rubric's 6 rules | goal-achievement | V1.4 pins all six verbatim, with replacement text |
| `consensus_verify.md:22-23` carries the same vocabulary | goal-achievement, conventions | in scope (rev 0 said "unless forced"; it is forced) |
| `case_contexts_are_split_into_follow_up_calls` imports the deleted constant | feasibility, conventions | V1.6 names the rewrite, not a supplement |
| `contexts[0]` trace attribution moves under coalescing | feasibility, testability | V1.5 decides it explicitly and pins it with a test |
| 4.17× ceiling is geometry-dependent | testability | §1.1 states the geometry and claims no average |
| `sync_templates()` does not write `.history.json` | feasibility | §3 separates copy from history edit |
| Doc-mirror list incomplete | conventions | V2.4 enumerates files and lines |
| `file-formats.md:997-998` `±2` belongs to `review_notes` | conventions | explicitly excluded; a "±2" grep would hit it |
| `cli.md` silently skipped | conventions | explicitly closed with the reason it cannot drift |
| `verify.py:44-47` module docstring goes stale | conventions | V2.4 includes it |
| Design rationale must live in a docstring, not a `#` comment | conventions | V1.1 states this |
| Union test not required to be mutation-proven | conventions | V1.6 requires it (maintainer decision) |

## Finding refuted — and the plan must not act on it

The conventions lens reported as **blocking** that `v019` would fail to reach
existing projects: v018-era copies match a shipped hash, so the overlay would
keep serving the **old** rubric, requiring a forced overwrite.

**This inverts the code.** `pipeline._load_template` (`pipeline.py:443`):

```python
if not path.is_file() or _is_shipped_leftover(path, name):
    path = _SKILL_TEMPLATES / name
```

A project file byte-identical to **any** shipped revision resolves to the
skill's **current** copy. Its docstring (`:438-440`) states the intent: *"that
second case is what lets a shipped prompt improvement reach an existing
project."*

Acting on the finding would have meant force-overwriting every project's
template — consent-gated, destructive, and aimed at a premise that is backwards.
The D2 design exists precisely to make this reach unnecessary.

## Framing finding (the one that changed the plan's shape)

The goal-achievement lens noted the maintainer's stated lever is **quality**,
that cost is explicitly a non-constraint, and that rev 0 excluded
`CONTEXT_WINDOW = 2` — the one knob that plausibly affects judgment, since it
determines how much context the model has to judge a sense against. The plan
sold a cost fix with the quality lever scoped out, justified by a probe about
call counts.

**Maintainer decisions, 2026-10-12, before any implementation:**
reframe around evidence quality and drop the provider-config derivation;
leave `±2` alone so one variable moves at a time; require mutation-proven tests
and mark the book A/B outstanding rather than blocking.

## Process note — one static round, spent

`workflows/verification-loop.md` allows **exactly one** static round. This was
it. Rev 1 was rewritten against its findings, and I did **not** re-run the
static lenses on the rewrite: doing so would spend a second round the loop does
not grant, and the rewrite's claims are testable by the P3 dynamic audits rather
than by re-reading. The correction to my own earlier statement to the user — that
I would "rerun the static round on the rewrite" — is recorded here rather than
quietly dropped.

## Gate

`uv run tests/run_all.py` at `c720c2e` → **54 passed, 0 failed, 2673 checks**,
verified live in the main tree with no ambient `config.local.json`. No code has
been changed; this round is plan-only.
---

## P3 V1 — context blocks + explicit byte cap — `goal_holds: true`

Commit scope: 6 modified + `v019.py`. Gate after the fix round: **54 suites /
2691 checks / 0 failed** (baseline 2673; V1 added 18 checks, removed none).

### The change

Stage 2 emitted one ±2 context per hit line, so a term with clustered hits made
the model re-read the same sentence up to eight times. V1 forms **blocks** —
overlapping-or-touching windows merged, union-preserving, chapter-scoped, formed
**before** batching — and replaces `MAX_CONTEXTS_PER_CALL` with
`BATCH_CHARS = 8 × (2 × CONTEXT_WINDOW + 1) × 2 × CONTEXT_CHARS` = 32,000.

Measured on `example/`, taken before any code moved: **8,414 contexts → 3,397
blocks** (−59.6%), 42,070 → 28,239 lines (−32.9%, **1.49×**).

**Three plan-level errors this phase caught, all of them mine:**

1. **The first measurement was wrong.** An early run reported **12.82×** by
   merging a term's hits *across chapter boundaries* — line indices restart at 0
   per chapter, so unrelated hits coalesced into meaningless blocks. Chapter-scoped
   re-run: **1.49×**. A unit error turned a 33% saving into a fabricated 93% one,
   and the output looked entirely plausible. Same class as the no-op that shipped
   behind a green gate: a confident number that is wrong.
2. **The dispatch brief contradicted the plan.** `PLAN:305` already said "V1
   ships `v019`"; the brief told the implementer the history edit was V2's. It
   escalated rather than working around it — the discipline earning its keep.
   `test_template_assets.py:73-80` forces template-edit ⟷ history-update to be
   atomic, so V1 could not otherwise be green.
3. **`BATCH_CHARS` scope was ambiguous.** "No batch exceeds BATCH_CHARS" does not
   say whether the rendered prompt or the evidence counts. Pinned to **evidence**
   (chapter name, line range, source/translation lines); the rubric, term block and
   language names are constant per call and uncounted, because the derivation's own
   arithmetic counts lines.

### Audit round

`goal_holds: true`, no blocking or major findings. The auditor built its own
32-case harness rather than reusing the implementer's fixtures; the union
invariant held on every case — first/last-line clamping, two-chapter terms,
distance-5 vs distance-6, consecutive hits, empty term, `variants: None`,
substring-of-a-longer entry, missing `translated/`.

**Accepted and fixed:**

- **F1 — the rubric named one thing two ways.** Line 7 said `block(s)` while the
  body said "context" in six places, the placeholder was `{{contexts}}`, and the
  deliberately-untouched `consensus_verify.md` said "context" throughout. A
  verdict-controlling rubric should not make the model map between two names for
  one unit. Unified on **context**; the `{{hit_count}}` half stays, because rules
  3 and 4 reason about occurrences.
- **F2 — the touch boundary was untested.** The `+1` mutation was not isolable:
  no fixture placed hits at the distance where touching and overlapping differ.
  Added `case_five_lines_apart_merges_and_six_does_not`; the mutation now turns
  **exactly one check red**, and the distance-6 control half stays correct under
  both conditions, which is what makes the first half attributable.

### Refuted — and it is the second time this exact claim has surfaced

The audit reported a pre-existing minor defect: that `.history.json`'s two older
`glossary_verify.md` digests do not match the real blobs, leaving
`_is_shipped_leftover` degraded and old rubric copies stuck as customizations.

**Refuted.** Hashing raw blobs through `git cat-file` — the byte path, not a
shell pipeline:

```
35789b7 -> e47706c8...  recorded: yes
04d9d98 -> 7949dc00...  recorded: yes
current -> 0fee31a4...  recorded: yes
```

All three match; no file contains a CR, so line-ending normalisation is not in
play. The auditor's mismatching hashes came from hashing a re-encoded stream.

**This is the same claim that arrived in P1 from the opposite direction.** There,
a lens reported that `v019` would not reach existing projects and recommended a
forced overwrite; the orchestrator refuted it on `_load_template` (`pipeline.py:443`),
correctly. This audit concluded the same practical outcome — old copies stranded —
for a different and wrong reason. Two reports, both wrong about the same fact, for
different reasons, with opposite conclusions that would have converged on the same
destructive action. Re-hashing raw bytes is what settled it.

### History — three released revisions

```
e47706c8...  original
7949dc00...  v018-era
0fee31a4...  current (context naming)
```

A fourth digest, `4df3f410...`, existed only on this unmerged branch. Append-only
exists so a revision some project may hold is never re-hashed away; those bytes
were never released, so no project can hold them, and shipping them would put a
revision into the history that was never shipped. **Removed rather than shipped**,
by maintainer ruling. `e47706c8...` and `7949dc00...` unchanged in value and order.

### Scope note

`consensus_verify.md` was deliberately left unchanged. Read against how blocks
actually behave, its rules 1-3 ("one term and several contexts, possibly in
different chapters"; "the contexts must be compared against each other"; "every
context here reached the job because…") all remain true, the arbitrator reads the
new span definition inside `<Original task prompt>` on every call, and every
candidate edit would have reworded a numbered rule — the thing V1.4 exists to
forbid.

### Carried to V2

Doc mirrors are knowingly stale against the code and assigned to V2.4:
`references/file-formats.md:1901`, `:1910` (names the deleted
`MAX_CONTEXTS_PER_CALL`), `:2127`; `SKILL.md:801/803`; `README.md:502/506`;
`references/maintenance.md:337-340`.