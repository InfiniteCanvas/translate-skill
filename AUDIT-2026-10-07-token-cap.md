# AUDIT — `PLAN-2026-10-07-token-cap.md`

Date: 2026-10-07
Target: `PLAN-2026-10-07-token-cap.md`
Method: independent verifier subagent pass **+** the parent's own code check, merged.
Rule for this audit: **grade against the plan's body, not its summary.** A defect the
plan acknowledges in a footnote still counts if implementing the plan as written walks
into it.

---

## VERDICT

**MAJOR REVISION REQUIRED** — both passes reached this independently.

The plan's diagnosis is correct and design C is the right shape. But four defects stand
between the plan and working code, two of which ship a silent quality regression. In the
exact configuration design C exists to serve, the plan's own formula makes the
corrective retry send **half** the cap of the attempt it is retrying — verbatim the
self-harm `v009.py` was written to prevent — and the proposed one-line `client.py` clamp
breaks a promise `consensus.py` documents in its own docstring.

The design survives. The change list does not.

| # | Sev | Finding | Found by |
|---|---|---|---|
| C1 | CRITICAL | `_pack_chunks` conflates packing budget with wire cap; both readings break | both |
| C2 | CRITICAL | Escalation formula de-escalates below the first attempt | both |
| C3 | CRITICAL | Low provider cap collapses `room` negative → fail-fast / over-fragmentation | subagent |
| M1 | MAJOR | `client.py` clamp breaks the consensus-synthesis invariant | subagent |
| M2 | MAJOR | Omitting `max_tokens` on one translator block re-tightens the whole job | subagent |
| M3 | MAJOR | Migration reasoning contradicts the plan's own end state | both |
| M4 | MAJOR | `client.py` line crashes on `max_tokens: null`, which the schema permits | subagent |
| M5 | MAJOR | Escalation guard must be re-keyed; §6 row 5 says the opposite | parent |
| m1–m6 | MINOR | Over-scoped test list, uncounted call sites, unwarnable block identity, 4 missed doc sites, wrong credit-lever claim, hardcoded 8192 | both |

---

## CRITICAL

### C1 — `_pack_chunks` conflates two different jobs

`_pack_chunks` uses `max_out` for the sizing budget **and** as the wire cap:

```python
# pipeline.py:813-814
budget = max_out * 4 // 5
room = budget - 256
# pipeline.py:827, 833-834
plan.append((lo, i, escalated if cost > budget else max_out))
# pipeline.py:1289, 1330
lo, hi, call_max_tokens = plan[k]
... max_tokens=call_max_tokens,
```

Plan §6 row 2 says "pack against `pack_cap`, send `wire_cap = max_out`" but gives no
signature, and the function cannot express the split. Both implementable readings fail:

- **Pass `pack_cap`** (no signature change) → per-chunk cap is `pack_cap`; every block
  gets 128,000; MiniMax is clamped to exactly what §3 says it must not get. §4's
  "MiniMax → 256,000 ✓" is false on the **normal** path, not just the retry.
- **Pass `wire_cap`** → packing sized against 256,000 (chunks to 204,544 chars) while
  Z.AI returns 128,000. The original bug, unchanged.

This single tuple element is load-bearing for the whole design precisely *because*
TRANSLATE is the only job that passes a cap.

**Fix.** `_pack_chunks(source_lines, pack_cap, escalated, wire_cap)`, third element
`escalated if cost > budget else wire_cap`. Then re-pin `test_chunking.py` `3e`/`4c` to
the **wire** value. Note the blast radius the plan omits: `tests/test_chunking.py` calls
`_pack_chunks` **directly with three arguments** at lines **228, 229, 249, 259**.

### C2 — the escalation formula de-escalates

`pipeline.py:1396-1397` states the invariant in its own comment: *"retry once at the
escalated cap **(>= the first attempt's)**"*. `_escalated_cap`'s `max(...)` guard exists
only to enforce it.

The plan defines `escalated = max(pack_cap, min(round(pack_cap*1.5), provider_max))`
(§4). Verified by execution on the plan's own motivating config
(`top_level=256000`, blocks `[128000, 256000]`):

```
config              pack  budget    room  plan_esc  fixed_esc  plan<wire
user (256k/128k)  128000  102400  102144    128000     256000       True
default shipped    65536   52428   52172     65536      65536      False
```

`pack_cap == provider_max` whenever the top level is at or above the tightest block —
which is true of the user's config and of **every** config where the ceiling binds. So
`_escalated_cap` collapses to a no-op, and the retry sends **128,000 after a 256,000
send**. That is verbatim `v009.py:10-14`: *"guaranteeing the same truncation and burning
a full attempt for nothing."*

Two plan claims die: §4's *"escalation still has somewhere to go"* (false for the
motivating config) and §6 row 5's *"the `max(...)` guard is now redundant but harmless"*
(the guard is what **produces** the downgrade).

**Fix.** Key the guard on the value actually sent:

```
escalated = max(wire_cap, min(round(pack_cap * 1.5), provider_max))
```

Verified: user config → `max(256000, 128000) = 256000`, no de-escalation. Default
shipped → `65536`, **identical to today**, so no regression on shipped defaults.

Corollary the plan must state: under design C the "escalate on over-budget chunk" branch
(`pipeline.py:827`) collapses to a **no-op** whenever `provider_max <= top_level`,
because there is nowhere above the ceiling to escalate to. Correct, but a semantic
change to the third tuple element — to document, not to discover mid-implementation.

### C3 — a low provider `max_tokens` collapses packing

The oversized-line guard uses `escalated` as its threshold (`pipeline.py:820`:
`if c + 256 > escalated`). With the plan's C2 formula and a low block, verified:

```
config              pack  budget    room  plan_esc  fixed_esc  room<0
low block (100)      100      80    -176       100      65536     True

line cost 15, oversized check is (cost+256) > escalated:
  plan formula : 271 > 100  -> raises ValueError? True
  fixed formula: 271 > 65536 -> raises ValueError? False
```

So on `tests/test_cleanup_flow.py`'s own fixture (block at 100, `pipeline.py:320`), the
plan's formula makes `_pack_chunks` **raise for every line** — zero LLM calls, the
chapter cannot translate at all. Today it translates badly; under the plan it cannot
translate.

C2's fix removes the `ValueError`, but `room = -176` **remains negative**, and a
negative room closes a chunk on every line, so any cap under `320` fragments the chapter
into one chunk per line. This is the direct cost of §4's claim that "a block below the
ceiling becomes the supported case" — the plan never asks what happens when the block is
below `256 + line_cost`.

**Fix.** Document a minimum supported provider `max_tokens` (≥ 320 for packing not to
degenerate; realistically ≥ 8192), and state it in `references/file-formats.md`, which
`AGENTS.md:14-16` names as the normative mirror.

---

## MAJOR

### M1 — the `client.py` clamp breaks the consensus-synthesis invariant

This is the finding that most undermines design C's placement of the clamp.

`consensus.py:112-113`, in its own docstring:

> "The consensus call reuses the task's json_schema and **never caps below either the
> task's explicit max_tokens or the consensus block's own max_tokens**."

Enforced by `consensus.py:201`: `c_max = max(max_tokens or 0, int(cblock.get("max_tokens") or 0)) or None`.

The plan §5 presents the clamp as *protecting* this call. It does the **opposite** — it
makes synthesis unable to exceed its block. In the tracked
`config.local.example.mixed.json`, the translator runs at 256,000 while `consensus` is
`glm-5.3 @ 65536`:

| | today | design C |
|---|---|---|
| `c_max` | `max(256000, 65536)` = 256,000 | `max(256000, 65536)` = 256,000 |
| wire | `256000 or 65536` → **256,000** | `min(256000, 65536)` → **65,536** |

A full-chapter TRANSLATE fan-out merges two complete chapter translations under a cap
**a third** of the task's contract. `consensus.py:203` returns that string with no
truncation check, so it surfaces as a TRANSLATE parse failure that then retries at the
broken C2 cap.

Worse for the suite: `test_consensus.py:294-308` (`b7 … never drops below the task cap`)
passes **because `FakeChat` replaces `client.chat` and never clamps**. The plan says
b4/b7 "should pass" — true, and that is the problem: **the suite goes green while the
invariant those checks document is dead at the wire.**

**Fix.** The clamp needs an opt-out for the synthesis call (an explicit parameter, or
route the synthesis cap through a path that bypasses it), **and** `consensus.py:112-113`
and `:198-200` updated — the plan lists neither as a change.

### M2 — omitting `max_tokens` on one translator block re-tightens the whole job

`pipeline.py:1253-1256` already folds the default into the minimum:

```python
provider_max = min(int(b.get("max_tokens") or config.DEFAULT_MAX_TOKENS) for b in ...)
```

With `pack_cap = min(top_level, provider_max)` as the packing input, a translator block
that **omits** `max_tokens` contributes 65,536 as a floor. Add a third block and forget
the key → `pack_cap = min(256000, 65536) = 65536`, and the entire chapter re-packs
against 64k because of an omission. Today that omission only affects that one block's
own send.

The plan's §11 "Non-TRANSLATE job caps — already correct" and §6 give this no thought.

**Fix.** Decide whether `provider_max` should skip blocks that omit the key, or
document the coupling and add a test either way.

### M3 — the migration reasoning contradicts the plan's own end state

Plan §9 argues no `v012` is needed because v009's bar was *"provably self-harm in the end
state"*, and under design C a block below the ceiling is no longer harmful.

**C1–C3 show the end state does contain provable self-harm**, in exactly the population
§9 dismisses. The two paragraphs contradict each other.

The *conclusion* (no migration) is still defensible — but on the **literal rule**, not the
harm test. `AGENTS.md` triggers on *"adds, removes, renames, or re-keys entries in the
project `config.json` schema … or modifies the skill-shipped prompt templates."* Change C
does none of those; only comments in `config.py` change. Drop "no provably-harmful end
state" as the justification.

Secondary: `config.py:93-99` is itself the normative statement of the invariant the change
makes false —

> "a provider default BELOW the translate cap would clamp the corrective retry below the
> first attempt — the exact defect v009 was written to close"

Plan §6 row 7 says only "update the two invariant comments". Under C that comment becomes
**true again by a different mechanism** (the client clamp). The edit must say why, not
just drop the clause.

### M4 — the proposed `client.py` line crashes on inputs the schema permits

Plan §5: `block_max if max_tokens is None else min(max_tokens, block_max)`.

`references/file-formats.md:303-306`: provider sampling knobs *"are **not** validated the
same way"*. `_with_defaults` (`config.py:176-181`) passes authored values straight
through, so a block may carry `"max_tokens": null` or a string. `min(int, None)` raises
`TypeError`; today's `or` merely sends `null`.

The rest of the repo defends against exactly this — `pipeline.py:1254` and
`consensus.py:201` both use `or <default>`. The plan's one-liner drops the established
idiom. Also, today's `or` treats `max_tokens=0` as unset; the plan's `is None` sends `0`.

**Fix.** `block_max = int(provider_cfg.get("max_tokens") or config.DEFAULT_MAX_TOKENS)`
before the `min()`.

### M5 — §6 row 5 is backwards

*"`_escalated_cap` — **leave the formula alone.** … the `max(...)` guard is now redundant
but harmless."*

Not redundant: it is load-bearing, and with the plan's `pack_cap`/`wire_cap` split it
becomes actively harmful (C2). Leaving it while adding the clamp is precisely the
configuration that ships the de-escalation. This also contradicts the plan's own §7,
which lists the test pinning that guard for rewrite.

*(Parent's note: the "don't slim verified code unprompted" preference does not apply
here — this is not slimming, it is a guard that must be re-keyed or it breaks the
invariant its own comment asserts.)*

---

## MINOR

- **m1 — the `test_chunking.py` rewrite list is over-scoped.** `test_chunking.py:191`
  writes `{"providers": {}, "translate_max_output_tokens": 500}`, so blocks resolve to
  `DEFAULT_MAX_TOKENS = 65536`; `pack_cap = min(500, 65536) = 500` and
  `escalated = max(500, min(750, 65536)) = 750` — **identical to today**. Budget 400,
  room 144, bounds `(0,4),(4,8)` unchanged, so `3e`/`4c`/`4g` and the
  persistence/feedback slices pass untouched. Only the docstrings (`:7-13`, `:40-42`)
  need edits. Listing five cases for rewrite costs review time and hides C1/C3.
- **m2 — `test_chunking.py:259`** calls `_pack_chunks(small, 8192, 12288)` — 8192 is
  pre-v009. Harmless, but the plan flags the stale `:72-73` comment and not this.
- **m3 — "sixteen chat call sites" is 14, and §2's list omits two.** §2 enumerates
  glossary ×4, reviewer, annotator, recap, profile, tn_recheck — omitting
  `review.py:216` and `review_notes.py:260`. The *substantive* claim (only
  `pipeline.py:1330` passes a concrete cap) is confirmed.
- **m4 — §6 row 3's reworded warning "cannot name the block that bound it."**
  `_warn_token_cap(provider_max, max_out)` receives only the `min` over blocks
  (`pipeline.py:781-789`). Naming the binding block needs a call-site change at
  `pipeline.py:1257` the plan does not list. Worse: under C the condition
  `provider_max < max_out` marks the **normal, supported** configuration, so the user's
  own config prints a `[warn]` every run. Severity should drop from `[warn]` to
  `[info]`, and `file-formats.md:313-320` / `SKILL.md:218-223` /
  `test_cleanup_flow.py:358-361` all pin the literal string.
- **m5 — four doc sites the plan's §8 misses.** `file-formats.md:1416-1419` gives the
  escalation formula as `min(round(1.5 × cap), smallest max_tokens)` and asserts
  "packing and the escalation budget against the minimum" (both change);
  `file-formats.md:1409-1411`'s `floor(0.8 × the cap)` (the pack cap is now a different
  value from the cap sent); `consensus.py:112-113` and `:198-200` (broken by the clamp —
  see M1). Separately, `file-formats.md:318-320` already claims *"chunk packing packs
  against it"* — **false today**, **true under C**. That is the strongest pre-existing
  doc/code divergence in the file, and design C is its fix.
- **m6 — the §8 credit-lever claim is wrong for the protected model.** Per
  `config.local.EXAMPLES.md:244-249`, Z.AI bills credits derived from tokens, but
  **MiniMax bills a flat monthly token pool** independent of the model named. Cutting
  MiniMax's block cap saves nothing and only truncates. The "new cost lever" is real for
  one half of the array and actively harmful for the other.

---

## VERIFIED CORRECT (checked; no action)

Recorded so the next reviewer does not re-derive them.

- **§1's diagnosis is right and is the plan's strongest contribution.**
  `references/file-formats.md:236` states *"must stay <= the translator blocks'
  max_tokens"*. Under that invariant the override at `client.py:170` is a no-op, so the
  defect is an **unenforced** invariant, not a wrong override.
- **Baseline is green:** `uv run tests/run_all.py` → 46 scripts, 1925 checks, 0 failed,
  47.6s, hermetic.
- **§2's scope restriction is right:** only `pipeline.py:1330` passes a concrete cap.
- **§3's arithmetic is right:** uniform clamping to 128,000 falls below the `>= 131072`
  threshold `test_sync_config.py:793` itself enforces, so the non-convergence claim
  holds. Design C's per-block outcome (glm-5.3 → 128,000; MiniMax → 256,000) is
  confirmed by hand.
- **`consensus.py:201` needs no *formula* change** — M1 is about the clamp, not the
  `max()`.
- **Doc citations are accurate.** Spot-checked `README.md:722-728`, `SKILL.md:204-208`,
  `file-formats.md:236`, `test_sync_config.py:804-818`, `v009.py`, `AGENTS.md:54-56`.
- **§8's "no edit" for the three example JSONs is correct** — all sit on the equal case.
- **`AGENTS.md:54-56` is stale** (claims next is `v010.py`; head is 11). Pre-existing,
  unrelated to this change, but should be fixed in the same pass.

---

## REQUIRED BEFORE IMPLEMENTATION

1. Split `_pack_chunks`; state the new signature and the third-element contract (C1).
2. Re-key `_escalated_cap`'s guard on `wire_cap`; **reverse §6 row 5** (C2, M5).
3. Document a minimum supported provider `max_tokens` and the negative-`room`
   degeneration (C3).
4. Give the consensus-synthesis call a clamp opt-out and update `consensus.py:112-113`,
   `:198-200` (M1).
5. Decide whether `provider_max` skips blocks that omit `max_tokens` (M2).
6. Fix the `client.py` line to use the repo's `or <default>` idiom (M4).
7. Commit to a named test for the clamp — `test_client_cache.py` has **zero**
   `max_tokens` matches, and `test_consensus.py`'s fakes bypass `client.chat` entirely,
   so the suite would go green with the invariant dead.
8. Restate the migration decision on `AGENTS.md`'s literal triggers (M3).

---

## UNVERIFIABLE FROM THE REPO

- **The motivating config is not in the repository.** No `config.local.json` exists in
  the workspace (gitignored). The tracked `config.local.example.mixed.json` has **both**
  translator blocks at 256,000. The plan's motivating config
  (`256000` / `[128000, 256000]`) comes from the maintainer and could not be confirmed
  here — and C1–C3 all depend on it. **Confirm the real config before implementing.**
- **Whether Z.AI silently clamps at 128,000 or hard-400s above it.** `docs.z.ai` is
  JS-rendered; the parameter table did not survive a plain fetch. Design C never sends
  a value above a block's declared ceiling either way, so the plan does not depend on
  the answer.
- **The MiniMax non-convergence figures** (19m51s, `finish_reason=length`) are the
  maintainer's measurements. `test_sync_config.py:787-796` encodes the conclusion; the
  raw timings are not recorded in the repo.