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