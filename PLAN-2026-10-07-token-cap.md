# PLAN — decouple `translate_max_output_tokens` from provider `max_tokens`

Date: 2026-10-07
Status: **proposed, not implemented.** No code has been changed.
Revision: **rev 2 — post-audit.** Every change from rev 1 is driven by
`AUDIT-2026-10-07-token-cap.md` (verdict: MAJOR REVISION REQUIRED). Rev 1's §6 change
table shipped two defects; they are marked `[rev2]` below.
Audit: `AUDIT-2026-10-07-token-cap.md`.

---

## 1. The defect

`references/file-formats.md:236` states the invariant the schema was designed around:

> `"translate_max_output_tokens": 65536, // … must stay <= the translator blocks' max_tokens`

**Under that documented invariant the override is a no-op.** If
`translate_max_output_tokens <= every translator block's max_tokens`, then
`client.py:170`'s `max_tokens or provider_cfg["max_tokens"]` sends `max_out`, which is
already ≤ every block's own ceiling. The override never raises anything in a
correctly-configured project.

The defect is that the documented invariant is **unenforced prose**. When it IS
violated, the failure is silent and expensive:

- packing sizes chunks against `floor(0.8 × top_level)` (`pipeline.py:813`), so chunks
  are built larger than the smallest block can return;
- the wire sends `top_level` (`client.py:170`), above that block's real ceiling;
- the response truncates or hard-400s;
- `_escalated_cap` (`pipeline.py:778`) returns the same value again — the corrective
  retry re-sends at the **same** oversized cap, guaranteeing a second identical
  failure;
- one advisory line prints and the run continues.

**The change is therefore: turn a documented "must" into a mechanically-guaranteed
"cannot violate".** Provider `max_tokens` becomes a per-provider ceiling; the top-level
key becomes a shared ceiling it can lower but never raise.

---

## 2. Scope

**TRANSLATE's cap resolution only.** Only one of the fourteen chat call sites passes an
explicit cap: `pipeline.py:1330` (TRANSLATE). `glossary` (×4), `reviewer`, `annotator`,
`recap`, `profile`, `tn_recheck`, plus `review.py:216` and `review_notes.py:260`, all
omit it and already use their own block's value. Unchanged. `[rev2: rev 1 said
"sixteen" and omitted the last two.]`

One coupling that matters: `config.py:206-225` gives every unauthored job the
translator's **whole array** (line 218) and an unauthored `consensus` the translator's
**first block** (line 216). So translator `max_tokens` values can reach other jobs
through inheritance. All real jobs are authored in the maintainer's config, but it is
why the clamp belongs in the client rather than only in the pipeline — with one
exception now carved out (§4.4).

---

## 3. The central tension — reasoning budget

`config.local.EXAMPLES.md:183-188` records the measured best-quality translator config
as `reasoning_effort: max` at a **256,000** cap, because `max`-depth reasoning needs
that headroom. A measured failure guarded at `tests/test_sync_config.py:787-796`:
Flash-Preview at `max` with a 65,536 cap does not converge — ~19m51s,
`finish_reason=length`, no content.

So two requirements are simultaneously true:

1. glm-5.3 must never be sent above **128,000**.
2. MiniMax at `max` effort should keep **256,000**.

**A single uniform cap cannot satisfy both.** Uniform clamping to 128,000 hands MiniMax
128,000 — 128,000 is below the `>= 131072` threshold the repo's own test enforces, so it
re-creates the measured non-convergence. This is the finding that rejects the naive fix.

---

## 4. Design

### 4.1 The three quantities `[rev2]`

Rev 1 conflated two. They are distinct and must be named separately:

```
top_level    = cfg["translate_max_output_tokens"]        # shared ceiling
provider_max = min(block.max_tokens for translator)      # tightest block
pack_cap     = min(top_level, provider_max)              # SIZING budget only
wire_cap     = top_level                                 # CEILING handed to _chat
escalated    = max(wire_cap, min(round(pack_cap*1.5), provider_max))
```

`pack_cap` answers "how much output can **every** block return" — it sizes chunks.
`wire_cap` answers "how much may **this** block be asked for" — each block clamps it
down to its own ceiling. They are different questions and were never the same number.

### 4.2 Design A — uniform clamp: **rejected**

`effective = min(top_level, min(blocks))` used for packing *and* the wire.
glm-5.3 → 128,000 ✓, MiniMax → 128,000 ✗ **violates §3**.

### 4.3 Design B — pure divorce, no override: **rejected**

Packing uses `pack_cap`; each block sends its own `max_tokens` verbatim.
Correct for the maintainer's config, but it deletes the top-level key's ability to *lower*
a runaway block — the documented lever for constraining reasoning blowup, which
`AGENTS.md` forbids removing casually. It also kills `_escalated_cap` outright.

### 4.4 Design C — ceiling + per-block clamp: **recommended**

`client.chat` turns `wire_cap` into a per-block value by clamping to that block's own
`max_tokens`. On the maintainer's config:

- glm-5.3 → `min(256000, 128000)` = **128,000** ✓
- MiniMax → `min(256000, 256000)` = **256,000** ✓
- packing budget → `floor(0.8 × 128000) − 256` = **102,144 chars** — both finish ✓

**Placement — with one carve-out `[rev2]`.** Rev 1 put the clamp unconditionally in
`client.chat` and claimed that protected the consensus-synthesis call. It does the
opposite. `consensus.py:112-113` promises the synthesis *"never caps below either the
task's explicit max_tokens or the consensus block's own max_tokens"*, enforced by
`c_max = max(...)` at `consensus.py:201`. In `config.local.example.mixed.json` the
translator runs at 256,000 while `consensus` is `glm-5.3 @ 65536`, so an unconditional
clamp drops the synthesis from **256,000 → 65,536** — merging two full chapter
translations under a third of the task's contract, with no truncation check at
`consensus.py:203`.

So the clamp is **opt-in per call site**, not global:

```python
# client.chat signature gains a flag
def chat(provider_cfg, prompt, json_schema=None, max_tokens=None,
         meta_hook=None, enforce_ceiling: bool = True) -> str:
    block_max = int(provider_cfg.get("max_tokens") or config.DEFAULT_MAX_TOKENS)
    cap = block_max if max_tokens is None else (
        min(max_tokens, block_max) if enforce_ceiling else max_tokens)
    body = {..., "max_tokens": cap}
```

`enforce_ceiling=False` at the synthesis call site (`consensus.py:203`) only.

The `or config.DEFAULT_MAX_TOKENS` idiom is required, not optional:
`references/file-formats.md:303-306` states provider sampling knobs *"are **not**
validated the same way"*, so a block may carry `"max_tokens": null`. A bare
`min(max_tokens, block_max)` raises `TypeError` on that. `pipeline.py:1254` and
`consensus.py:201` both already use this idiom; rev 1's one-liner dropped it.

---

## 5. Changes `[rev2 — rev 1's table shipped CRITICAL-1 and CRITICAL-2]`

| # | File | Change |
|---|---|---|
| 1 | `lib/client.py:130, 170` | Add `enforce_ceiling` (default `True`); compute `block_max` with the `or DEFAULT_MAX_TOKENS` idiom; clamp `max_tokens` to `block_max`. `max_tokens is None` → byte-identical to today. |
| 2 | `lib/consensus.py:203` | Pass `enforce_ceiling=False` for the synthesis call only. |
| 3 | `lib/pipeline.py:792-835` | **Split `_pack_chunks`**: `_pack_chunks(source_lines, pack_cap, escalated, wire_cap)`; `budget`/`room` from `pack_cap`, third tuple element `escalated if cost > budget else wire_cap`. |
| 4 | `lib/pipeline.py:1249-1258` | Compute all four quantities per §4.1; pass them to `_pack_chunks`. |
| 5 | `lib/pipeline.py:772-778` | **Re-key `_escalated_cap`'s guard on `wire_cap`**, not `pack_cap`: `max(wire_cap, min(round(pack_cap*1.5), provider_max))`. **Rev 1 said "leave the formula alone — the guard is now redundant but harmless." That is backwards:** with the `pack_cap`/`wire_cap` split, the guard is what *produces* the de-escalation. `pipeline.py:1396-1397` asserts the invariant in a comment — "escalated cap (>= the first attempt's)" — and rev 1 would have broken it in the very config the change exists to serve. |
| 6 | `lib/pipeline.py:781-789` | `_warn_token_cap`: condition becomes "the ceiling is being lowered", and the message reports the effective cap. **Severity drops from `[warn]` to `[info]`** — under C the condition marks the *normal, supported* configuration, so the maintainer's own config would print a warning every run. `[rev2]` |
| 7 | `lib/pipeline.py:1249-1258` | Decide whether `provider_max` skips blocks that omit `max_tokens` (audit M2), then add a test either way. **Open question, §8.** |
| 8 | `lib/pipeline.py:1249-1258` | New guard: when a block's *effective* cap falls below its own declared cap **and** it uses `reasoning_effort` `high`/`max`, warn about the §3 convergence trap at runtime. |
| 9 | `lib/config.py:37-40, 93-100` | Rewrite both invariant comments. Note `config.py:93-99` states the very defect C2 fixes — under C it becomes **true again by a different mechanism** (the client clamp). The edit must say why, not just drop the clause. |
| 10 | `lib/consensus.py:112-113, 198-200` | Update the docstring/comments now that the clamp is opt-in. `[rev2]` |

### 5.1 Consequences that must be documented, not discovered `[rev2]`

1. **The over-budget escalation branch collapses to a no-op** when
   `provider_max <= top_level` — there is nowhere above the ceiling to escalate to.
   Correct, but a real semantic change to `_pack_chunks`' third element.
2. **A minimum supported provider `max_tokens`.** `room = budget - 256` goes negative
   when `pack_cap < 320`, so every line becomes its own chunk; below `256 + line_cost`
   the oversized-line guard at `pipeline.py:820` fires for every line and the chapter
   cannot translate at all. Verified by execution on `test_cleanup_flow.py`'s own
   fixture (block at 100 → `room = -176`). State a real floor (≥ 8192) in
   `references/file-formats.md`.

---

## 6. Tests

Baseline verified green: `uv run tests/run_all.py` → **46 scripts, 1925 checks, 0
failed, 47.6s**, hermetic.

### Rewrite `[rev2 — rev 1 over-scoped the chunking file]`

| Test | Locks | Change |
|---|---|---|
| `test_cleanup_flow.py:case_truncated_retry_cap` — 5a–5d | `5b` `calls == [max_out]*3` with block at 100; `5d` unit-pins all four `_escalated_cap` branches; `5c` the literal warn string; `5a/5b` both chapters translate | **Highest risk.** Asserts the override, the escalation guard *and* the warning at once; its fixture (block `100` vs top-level `65536`) is the exact adversarial case — and under §5.1 it is also the negative-`room` case. |
| `test_sync_config.py:804-818` | `smallest >= max_out` + comment "it overrides every block's own max_tokens" | **Inverts.** `smallest < max_out` becomes harmless. |

**`test_chunking.py` does NOT need a rewrite.** `[rev2 — rev 1 listed five cases in
error]` `:191` writes `{"providers": {}, "translate_max_output_tokens": 500}`, so blocks
resolve to `DEFAULT_MAX_TOKENS = 65536`, giving `pack_cap = min(500, 65536) = 500` and
`escalated = max(500, min(750, 65536)) = 750` — **identical to today**. Budget 400, room
144, bounds `(0,4),(4,8)` unchanged; `3e`/`4c`/`4g` and the persistence/feedback slices
pass untouched. Only the docstrings (`:7-13`, `:40-42`) and the stale `16384` comment
(`:72-73`, confirmed stale against `DEFAULT_MAX_TOKENS = 65536`) need edits. `:259`'s
hardcoded 8192 is pre-v009 and harmless.

### New coverage — the gaps that matter

1. **The `client.py` clamp has no test at all.** `test_client_cache.py` has zero
   `max_tokens` matches. This line is the enforcement point for the entire invariant.
2. **The clamp must not apply to the synthesis call** (audit M1). Note
   `test_consensus.py`'s `FakeChat` replaces `client.chat` entirely, so `b4`/`b7` pass
   **whether or not the clamp is correct** — the suite would go green with the invariant
   dead at the wire. Needs a test that exercises the real `client.chat`.
3. **`max_tokens: null` / string** in a provider block (audit M4) — permitted by
   `file-formats.md:303-306`, untested.
4. **`extra_body` precedence over the clamp** — documented in §8, enforced by nothing.
5. **The omitted-`max_tokens` re-tightening** (audit M2), once §7 decides the behavior.
6. **`DEFAULTS["translate_max_output_tokens"] <= DEFAULT_MAX_TOKENS`** — asserted in a
   comment at `config.py:93-99`, pinned by no test.
7. **The minimum provider cap** (§5.1) — a test that a sub-320 block is rejected loudly
   rather than silently fragmenting.

### Adjust

- `test_consensus.py` `a2`/`b1` — passthrough of the task cap; `b4`/`b7` pin
  `consensus.py:201`'s `max()`, unchanged, but see new coverage #2.
- `test_sync_config.py:787-796` (`>=131072` for Flash-Preview at `max`) and `:798-802`
  (`>=65536` every block) — **keep and extend**. They read the *example file*, not the
  effective wire value, so a project can pass them and still send a non-converging cap.
- `test_cleanup_flow.py:324` resets `_TOKEN_CAP_WARNED` — keep if the latch survives.

---

## 7. Docs

| Location | Edit |
|---|---|
| `references/file-formats.md:236` | Normative. "must stay <= the translator blocks' max_tokens" → the effective cap is the smaller of the two. Also add the **minimum supported provider `max_tokens`** (§5.1). `AGENTS.md:14-16` names this file the schema mirror. |
| `references/file-formats.md:1401-1404` | "Each translate call sends `max_tokens = translate_max_output_tokens`" → the clamped per-block value. |
| `references/file-formats.md:1409-1411` | `floor(0.8 × the cap)` — the pack cap is a **different value** from the cap now sent. `[rev2]` |
| `references/file-formats.md:1416-1419` | Gives the escalation formula as `min(round(1.5 × cap), smallest max_tokens)` and asserts "packing and the escalation budget against the minimum" — both change. `[rev2]` |
| `references/file-formats.md:313-320`, `SKILL.md:218-223` | Mirror the reworded, re-severitied `_warn_token_cap` marker. `[rev2]` |
| `references/file-formats.md:318-320` | Already claims *"chunk packing packs against it"* — **false today, true under C.** The strongest pre-existing doc/code divergence in the file; this change is its fix. `[rev2]` |
| `config.local.EXAMPLES.md:87-94` | Most directly contradicted sentence in the repo. |
| `config.local.EXAMPLES.md:192-195` | Plus "every translator block **must** be raised with it" — a block below the ceiling becomes the supported case. |
| `config.local.EXAMPLES.md:292-294` | Split: "must stay consistent" dies; "the SMALLEST block governs packing" becomes newly correct. |
| `config.local.EXAMPLES.md:90-91` | Literal claim survives, but a reader now draws the opposite inference — setting provider `max_tokens` alone now **lowers** the send. |
| `config.local.EXAMPLES.md:114-121` | **Gap created by this change.** `extra_body.max_completion_tokens` does not participate in the clamp (merges after; not a `max_tokens` key), so the server-side effective cap can be ambiguous. Must be stated explicitly. |
| `config.local.EXAMPLES.md:183-188, 196-197` | The all-three-equal recipe stays valid and stays necessary — only its justification changes. Do not simplify the example. |
| `config.local.EXAMPLES.md:239-276` | **Rev 1's "new cost lever" claim is wrong for MiniMax** (`[rev2]`). Per `:244-249` Z.AI bills credits derived from tokens, but MiniMax bills a **flat monthly pool**. Cutting MiniMax's block cap saves nothing and only truncates. Report the asymmetry; do not optimize for it (`AGENTS.md:36-41`). |
| `README.md:722-728`, `SKILL.md:204-208` | "per-call output cap" → "ceiling"; the packing formula's input becomes `pack_cap`. |
| `config.local.example.{zai,minimax,mixed}.json` | **No edit.** All three sit on the equal case, unambiguous under C. *(But: the mixed file's translator-at-256000 / consensus-at-65536 combination is what audit M1 fires on — the file is valid, the *interaction* is the bug.)* |
| Root `AGENTS.md:54-56` | Pre-existing defect: claims the next migration is `v010.py`; `v010.py` and `v011.py` both exist, head is 11. The operative rule is correct. Fix in this pass. |

---

## 8. Migration — **none** `[rev2 — restated on the rule's actual trigger]`

`AGENTS.md` triggers on *add / remove / rename / re-key* to `DEFAULTS` or
`PROVIDER_DEFAULTS`, or a template change. This change does **none** of those — no key,
no `DEFAULTS` value, no template. Comments in `config.py` change; that is not a trigger.
**No migration.**

*Rev 1 reached the same answer via "the end state is no longer provably self-harm". The
audit refuted that argument — the end state **does** contain self-harm (§5, audit C2),
in exactly the population rev 1 dismissed. The conclusion survives; the reasoning does
not. Do not restate it.*

What a `v012` would buy is a commit-line record of a silent behavior change for projects
carrying a deliberately-low block `max_tokens` (the population `v009`–`v011` all left
untouched — they only rewrite exact old defaults). Two ways to get that record without a
migration: note it in the README's migration-history section, or accept the silence.
**Open question for the maintainer.**

---

## 9. Decisions and implementation record

**Maintainer's answers, 2026-10-07 — all three resolved:**

1. **Omitted `max_tokens`.** Keep the existing behavior: an omission contributes
   `DEFAULT_MAX_TOKENS` (65536) to `provider_max`. An omission means "unset",
   not "unlimited", so one forgotten key must not silently resize the whole job.
   Documented in `config.py`; covered by `test_token_cap_ceiling.py` case f.
2. **Migration: yes.** Shipped as `v012.py` — deliberately **report-only**. It
   reads the raw config and explains a mismatched pair; it rewrites nothing,
   because a resolution-rule change has no old-default sentinel (unlike v009's
   16384/8192), and rewriting a user's chosen numbers because their *relationship*
   changed is exactly the silent edit v009's own docstring warns against.
   Verified against the maintainer's real config: one `[warn]` naming both
   numbers, the new per-part budget (102144 characters), the remedy, and the
   mid-translation restart consequence. Byte-identical file, idempotent.
3. **Real config confirmed** — `256000` / `[glm-5.3 @ 128000, MiniMax @ 256000]`,
   `consensus` authored at `128000`. That last detail settled audit M1: the
   synthesis genuinely needs the opt-out here, because the arbitrator's 128000
   is a third of what the candidates need to merge.

**Shipped:**

| Area | Change |
|---|---|
| `client.py` | `_resolve_cap()` + `enforce_ceiling` (default `True`); `or DEFAULT_MAX_TOKENS` idiom so null/0/string never raise |
| `consensus.py` | synthesis passes `enforce_ceiling=False`; docstrings updated on both sides |
| `pipeline.py` | `_pack_chunks` split (`pack_cap` vs `wire_cap`); `_escalated_cap` re-keyed on `wire_cap`; `_warn_token_cap` → `_note_token_cap` at `[info]`; `_check_translator_caps`; `MIN_TRANSLATOR_MAX_TOKENS` |
| `config.py` | both invariant comments rewritten (`:37-40` ceiling, `:93-100` fallback) |
| `migrations/v012.py` | report-only step; chain head now 12; root `AGENTS.md` version list corrected |
| `tests/` | new `test_token_cap_ceiling.py` (24 checks); `test_cleanup_flow` cap case rewritten to the real shape; `test_consensus` b8/b9 added; `test_sync_config` invariant inverted; `test_migrate` case_14 + head pin |

**Docs updated:** `references/file-formats.md` (`:236`, `:313-320`, `:1401-1419`,
migration history), `SKILL.md` (`:215-228`, migration history), `README.md`
(`:119-132`, `:722-730`), `config.local.EXAMPLES.md` (`:49-51`, `:87-94`,
`:119-131`, `:190-199`, `:297-300`), root `AGENTS.md`.

**Suite: 47 scripts, 1963 checks, 0 failed** (baseline 46 / 1925).

**Two defects the implementation surfaced, both now fixed:**

- The `v012` floor check was shadowed by the ceiling check, so an unpackable block
  (100) got the "raise your ceiling" advice instead of the "this cannot be packed
  at all" one. Reordered; `test_migrate` 14g pins it.
- Three of the four initial failures in `test_token_cap_ceiling.py` were wrong
  *expectations* in the new test, not code defects — a per-block clamp correctly
  returns different values for blocks on either side of the caller cap. Corrected.

**Still open, deliberately out of scope:** per-block escalation (escalating toward
each block's own ceiling rather than a shared scalar). The shared-scalar path is
now correct; this is a follow-up.

---

## 10. Blast radius

- **Code:** `client.py` (signature + clamp), `consensus.py` (opt-out + docstring),
  `pipeline.py` (4 sites + 1 new guard), `config.py` (comments).
- **Tests:** 2 files rewritten, 2 adjusted, **7 new cases** for the coverage gaps.
- **Docs:** 5 files, ~16 edits.
- **Migration:** none.
- **Operational:** a chapter mid-translation when `pack_cap` changes fails the
  crash-resume line-count check at `pipeline.py:1270-1276` and retranslates from
  scratch. This is **per in-progress chapter**, and it hits every project where
  `top_level > tightest block` — exactly the population §8 wants to leave silent. Worth
  a release note.

---

## 11. Explicitly out of scope

- Changing `DEFAULTS` or `PROVIDER_DEFAULTS` values.
- `extra_body` semantics beyond documenting its non-participation.
- The `translator`-array-inheritance coupling at `config.py:206-225` (real, documented in
  §2, separate change).
- Per-block escalation (escalating toward each block's own ceiling rather than a shared
  scalar) — follow-up, once the shared-scalar path is correct.
- Non-TRANSLATE job caps — already correct.