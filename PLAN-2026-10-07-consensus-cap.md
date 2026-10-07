# PLAN — a provider HARD output ceiling that binds the consensus synthesis

Date: 2026-10-07
Status: **proposed, not implemented.** No code has been changed.
Baseline: commit `745433e`. **`uv run tests/run_all.py` is NOT green at HEAD** —
see §1.1.
Revision: **rev 2 — post-audit.** `AUDIT-2026-10-07-consensus-cap.md` found two
arithmetically false claims (§5, §7.1) and six smaller errors, all folded in
below and marked `[rev 2]`. Two of the auditor's blockers were against a stale
read of rev 1 and needed no change; §2 of the audit explains which is which.

---

## 1. The defect, as observed

A real run (`run-20261007-205356-translate-2812197`, CHAPTER_0004.md) sent
`max_tokens: 256000` to a consensus block that declares `max_tokens: 128000`:

```
job=consensus  consensus_for=translator  model=glm-5.3  params.max_tokens=256000
  -> HTTP 400 {"code":"1210","message":"The max_tokens parameter is illegal.：限制数值范围[1,131072]"}
job=consensus  consensus_for=reviewer   model=glm-5.3  params.max_tokens=128000  -> OK
```

Same provider block, same model, two different sent caps. The translator's
synthesis overran Z.AI's hard `[1,131072]`; the reviewer's did not.

**Cost of the failure.** `consensus.py:221-230` catches it, logs a tier-1
`degraded` event, prints one `[warn]`, and returns candidate 1 **unmerged**. The
chapter translates, but the multi-model consensus merge never runs — a silent
quality loss, which `AGENTS.md` ("quality outranks quota cost") treats as the
thing most worth fixing. Three more identical HTTP 400s are burned first
(`client.py:294` counts a non-guided-JSON 400 as retryable, `_MAX_ATTEMPTS=4`
at `client.py:22`).

## 1.1 The baseline is red, and in exactly the file this change edits

`uv run tests/run_all.py` at `745433e`: **46 scripts pass, `test_sync_config.py`
fails with 3 checks**, all on `config.local.example.mixed.json`:

```
FAIL 10 ...mixed.json: Flash-Preview blocks on `max` carry a >=131072 cap
     [`max` effort under a cap that cannot hold it: ['annotator[MiniMax…]', 'translator[MiniMax…]']]
FAIL 10 ...mixed.json: every block carries the 64k output budget
     [blocks below 65536 max_tokens: ['annotator[1]', 'translator[1]']]
FAIL 10 ...mixed.json: translate_max_output_tokens is a usable ceiling
     [translate_max_output_tokens=256000 smallest translator max_tokens=0 (floor 8192)]
```

Both MiniMax blocks carry `reasoning_effort: max` but **no `max_tokens` key at
all** (`config.local.example.mixed.json:12-22` and `:49-59`). The last commit
`745433e "fixed mixed local example"` did not add them. The validator reads a
missing key as `0` (`test_sync_config.py:793,800,816`), not as the runtime's
`DEFAULT_MAX_TOKENS`, so it reads as 0 — below the 8192 packing floor and below
the 131072 that `max`-effort reasoning is measured to need
(`test_sync_config.py:782-786`).

So the shipped example fails on its own contract **and** contradicts
`config.local.EXAMPLES.md:195-198`, which states the file "runs the translator
at `max` with a 256,000 cap (`max_tokens`, `max_completion_tokens`, and
`translate_max_output_tokens` all 256000)". Only the last two are.

This is **in scope**: `AGENTS.md` requires the suite to pass before a task is
called done, and the fix lives in the one file this change already edits. It is
recorded here rather than silently absorbed, because it is a pre-existing defect
with a cause nobody asked about (see §7.1).

## 2. Root cause

`consensus.py:214`:

```python
c_max = max(max_tokens or 0, int(cblock.get("max_tokens") or 0)) or None
```

with `enforce_ceiling=False` at `consensus.py:218`, which makes
`client._resolve_cap` (`client.py:150`) return the caller's value verbatim.

The `max()` is deliberate and documented (`consensus.py:205-213`): the
arbitrator must be able to merge candidates bigger than its own declared
budget, so a block configured at 65536 can merge two 256000-token chapters.
The opt-out exists for exactly the shipped `config.local.example.mixed.json`
shape.

The schema, however, asks one key to mean two different things:

| meaning | can it be raised? | example |
|---|---|---|
| the output budget this block **wants** | yes, freely — `max()` is the point | consensus 65536 merging 256000 candidates |
| the provider's **hard rejection threshold** | never | Z.AI rejects > 131072 |

`max_tokens` serves as the first everywhere. There is **no way to express the
second**, so a user who sets `consensus.max_tokens` to their provider's real
limit gets `max(task_cap, 131072)` and is sent straight past it. v012
(`03d1026`) fixed the symmetric problem for the *translator* by making
`translate_max_output_tokens` a ceiling — it never touched this one call site.

**This is not exotic.** The shipped `config.local.example.mixed.json` reproduces
it exactly: translator ceiling 256000, consensus `max_tokens` 128000 on
Z.AI/GLM-5.3 ⇒ `c_max = 256000` ⇒ HTTP 400 on every multi-model translator call.

## 3. The fix

Add an **optional** per-provider-block key, `max_tokens_limit`: the provider's
HARD ceiling. Absent ⇒ today's behavior byte-for-byte. Present ⇒ it bounds
**every** call to that block, including the consensus synthesis.

The floor stays where it is (`consensus.chat` keeps `max(task, block)`); the
**ceiling** becomes the block's to declare, and `client._resolve_cap` enforces
it. Layering matters: consensus decides how much room the merge needs, the
block decides how much the provider will accept.

### 3.1 Single source of truth

`config.block_cap(block) -> int` — the block's effective ceiling:

```python
def block_cap(block: dict) -> int:
    cap = int(block.get("max_tokens") or DEFAULT_MAX_TOKENS)
    limit = block.get("max_tokens_limit")
    return min(cap, int(limit)) if limit else cap
```

Both call sites use it, so they cannot drift:

- `client._resolve_cap` — `block_max = config.block_cap(provider_cfg)`;
  after computing `cap` by the existing rule, `if limit: cap = min(cap, limit)`.
- `pipeline.provider_max` (the packing minimum, `pipeline.py:1338-1341`) —
  `min(config.block_cap(b) for b in blocks)`.
- `pipeline._check_translator_caps` — `block_max` at `pipeline.py:836` becomes
  `config.block_cap(b)`. This is load-bearing twice over, not just for the
  "squeezed" heuristic at `:841`: a block declaring `max_tokens: 256000,
  max_tokens_limit: 8192` genuinely cannot return more than 8192, so the
  `MIN_TRANSLATOR_MAX_TOKENS` floor check at `:838` must fire on it. Using
  `block_cap` there makes that true automatically; leaving `:836` alone would
  report a block as 256000-capable while packing it at 8192.

### 3.1b A gap §4 alone would introduce — the "squeezed" warning goes blind

`_check_translator_caps`'s second warning exists because a reasoning model at
`high`/`xhigh`/`max` under a small cap does not converge — measured on
Flash-Preview: 64k at `max` ran ~19m51s and returned nothing
(`pipeline.py:826-831`, mirrored at `test_sync_config.py:782-786`). Its test is
`effective < block_max` (`pipeline.py:842`).

Once `:836` reads `block_cap`, that comparison becomes `effective < block_cap`.
For a block declaring `max_tokens: 256000, max_tokens_limit: 65536` at `max`
effort, `block_cap` is 65536, `effective` is 65536, the condition is **false**,
and the warning stays silent — for exactly the configuration measured to
return nothing. Using `block_cap` here without also fixing the comparison
would add a new way to hit the cliff undiagnosed.

The comparison's subject is the model's **own declared budget**, which is what
the warning is about; the limit is one of the two things that can lower it
below. So the rule becomes: compare `effective` against the block's declared
`max_tokens`, and name the limit as a second cause in the message when it is
the lower of the two.

Note this is **not** a regression for existing configs: a block with no limit
and `max_tokens: 65536` already produced `effective == block_max` and no
warning. The new key merely makes the state easier to reach, which is why it
is fixed here rather than deferred.

### 3.1c What `block_cap` does NOT cover

`extra_body` merges after the clamp (`client.py:218-222`) and can override a
key that is literally present. But every MiniMax block in the shipped examples
uses `extra_body.max_completion_tokens` (`config.local.example.mixed.json:19,56`)
— a **different parameter**, added *alongside* `max_tokens`, not instead of it,
and both are pinned as present at `test_token_cap_ceiling.py:173-179`.

So `max_tokens_limit` bounds the `max_tokens` field only. A provider that honors
`max_completion_tokens` sits outside this ceiling entirely — stated in the key
table rather than implied away. In the failing config the consensus block (GLM)
carries no `extra_body` at all, so the fix binds there; the guarantee is not
overstated for blocks that do.

### 3.2 Why packing must know about the limit

If a translator block declares `max_tokens: 256000, max_tokens_limit: 131072`
and packing ignored the limit, `pack_cap` would size parts for 256000 and
every call would truncate at 131072 — the exact silent-truncation trap v012
was written to kill. Feeding `provider_max` through `block_cap` makes packing
follow automatically, and `_escalated_cap` (`pipeline.py:798`) is correct for
free because it already takes `provider_max` as an input.

## 4. Scope

| # | Change | File |
|---|---|---|
| 1 | `block_cap()` helper | `scripts/lib/config.py` |
| 2 | `_resolve_cap` clamps by `max_tokens_limit` even when `enforce_ceiling=False`; docstring corrected — it names `pipeline._provider_max`, which does not exist (`provider_max` is a local at `pipeline.py:1338`) | `scripts/lib/client.py:130-150` |
| 3 | packing minimum uses `block_cap` | `scripts/lib/pipeline.py:1338-1341` |
| 4 | `_check_translator_caps`: `block_max` uses `block_cap` for the 8192 floor (`:838`), but the "squeezed" comparison keeps the block's **declared** `max_tokens` and the message gains the limit as a named cause (§3.1b) | `scripts/lib/pipeline.py:836-852` |
| 5 | repair the shipped example: add `max_tokens: 256000` to both MiniMax blocks (clears the 3 pre-existing failures) and `max_tokens_limit: 131072` to the consensus block | `config.local.example.mixed.json` |
| 6 | docstring/comment updates at `consensus.py:112-120,205-214` | `scripts/lib/consensus.py` |
| 7 | migration `v013.py`, report-only | `scripts/migrations/v013.py` |
| 8 | tests | `tests/test_token_cap_ceiling.py`, `tests/test_migrate.py` |
| 9 | docs (below) | see §7 |

`consensus.py:214` itself does **not** change. Its `max()` is correct; it was
only ever the missing ceiling that made it unsafe.

## 5. Behavior matrix

The synthesis and the fan-out reach `_resolve_cap` with **different** values, so
the table carries both:

- **fan-out** — `consensus.chat` forwards the caller's `max_tokens` verbatim
  (`consensus.py:150`), so `_resolve_cap` receives the raw task cap.
- **synth** — `consensus.chat` first raises it to
  `c_max = max(task_cap, block_max)` (`consensus.py:214`), so `_resolve_cap`
  receives that, already ≥ the block's own cap.

Then `_resolve_cap`: `block_max = block_cap(block)`; falsy cap ⇒ `cap =
block_max`; else `cap = min(task, block_max)` when `enforce_ceiling`, `cap =
task` when not; finally `if limit: cap = min(cap, limit)`.

| block `max_tokens` | limit | call | reaches `_resolve_cap` | sent before | sent after |
|---|---|---|---|---|---|
| 128000 | — | fan-out | 256000 | 128000 | 128000 |
| 128000 | — | synth | 256000 | 256000 | 256000 (unchanged) |
| 128000 | 131072 | synth | 256000 | 256000 | **131072** ← the bug |
| 128000 | 131072 | fan-out | 256000 | 128000 | 128000 (no-op) |
| 65536 | — | fan-out | 1000 | 1000 | 1000 |
| 65536 | — | synth | 65536 | 65536 | 65536 (unchanged) |
| 65536 | — | synth | 4096 | 4096 | 4096 (unchanged; pins `b7`) |
| 65536 | 2048 | synth | 65536 | 65536 | **2048** |
| 65536 | 131072 | synth | 65536 | 65536 | 65536 (limit above block: no-op) |
| 256000 | — | fan-out | 256000 | 256000 | 256000 |
| omitted | — | fan-out | — | 65536 | 65536 |
| 256000 | 131072 | fan-out | 512 | 512 | 512 |

Only the two bold rows change. The `limit ≥ block max_tokens` row is the one a
user is most likely to get wrong — writing a limit believing it raises the block
— and it is the branch where `min()` does nothing at all. Every existing pin
(`test_consensus.py` `b4`, `b7`, `b8`, `b9`; all of `test_token_cap_ceiling.py`)
must survive untouched.

`[rev 2 — rebuilt after the audit. Rev 1 had no `c_max` column and was wrong
twice: it read "task cap" as the value reaching `_resolve_cap` (false for every
synthesis row, where `max(task, block)` has already been applied), and it
claimed the translator example left `provider_max` unchanged (it doubles — see
§7.1). Both errors were mine and both were of the same kind: a value checked
after the edit and called unchanged without being checked before it.]`

## 6. Decisions taken, with reasons

1. **Optional key, no default in `PROVIDER_DEFAULTS`.** Absence must mean "no
   extra ceiling" so no project changes behavior. Adding it to
   `PROVIDER_DEFAULTS` would materialize a value into every block on every
   `migrate` — a behavior change disguised as a default.
2. **Applies to all calls, not just the synthesis.** A key named
   `max_tokens_limit` that is honored on exactly one call site is a trap: the
   user sets it on their translator block and nothing happens. §3.2 shows the
   cost of doing it coherently is two one-line call-site changes.
3. **No schema validation**, matching `max_tokens` exactly
   (`file-formats.md:305`: provider sampling knobs are read per request). A
   non-numeric `max_tokens_limit` raises the same `ValueError` a non-numeric
   `max_tokens` does (`client.py:147`). Parity beats a special case, and the
   audit confirmed parity is the status quo rather than a new exposure.
4. **Migration `v013` is report-only**, like `v012`. There is no old-default
   sentinel — no value is wrong that was right — and the fix needs no rewrite.

   Its trigger is *"the synthesis will send more than the consensus block's own
   declared `max_tokens`, and no limit is declared on that block"* — i.e.
   `max(task_cap, cblock.max_tokens) > cblock.max_tokens`. That is the one
   condition observable from disk without knowing the provider.

   `[rev 2 — rev 1 proposed "consensus declares N, the translator ceiling is M >
   N", which was wrong in both directions: it stays silent on a stock project
   (both at 65536, so `M > N` is false) which may be exactly the broken one, and
   it warns on `ceiling > consensus block`, a shape `file-formats.md:313-317`
   calls SUPPORTED since v012.]`

   The line is an `[info]`, never claims the provider will reject, and says
   plainly that the shape is supported — the report exists to name the one
   number the pipeline will send and let the user decide what their provider
   accepts.
5. **`extra_body` is not covered by this ceiling** — see §3.1c. It is the
   documented escape hatch, but `max_completion_tokens` is a different parameter
   and sits outside `max_tokens_limit`.

## 7. Documentation (the AGENTS.md mirror rule)

| File | Change |
|---|---|
| `references/file-formats.md` | provider-block key table gains `max_tokens_limit` (with the `max_completion_tokens` caveat and the consensus-inheritance note); the consensus bullet gains the hard-ceiling rule; the v013 entry in the Migrations narrative |
| `config.local.EXAMPLES.md` | document the key; correct the mixed example's cap claims at both `:195-198` and `:209-214` (the latter says packing is "about 200k at 256k" when `pack_cap = min(256000, 65536)`) |
| `config.local.example.mixed.json` | `max_tokens: 256000` on both MiniMax blocks; `max_tokens_limit: 131072` on the consensus block |
| `SKILL.md` | the one-line trigger, plus the v013 sentence in the migration narrative at `:585-599` |

### 7.1 Adjacent defect found while editing the example — now IN SCOPE

`config.local.example.mixed.json:12-22` and `:49-59` — both MiniMax blocks have
**no `max_tokens`**, so at runtime they resolve to `DEFAULT_MAX_TOKENS = 65536`
(`config.py:107`) while the shipped-example validator reads them as `0`
(`test_sync_config.py:793,800,816`). Either way they contradict
`EXAMPLES.md:195-198` and fail three checks (§1.1).

Adding `"max_tokens": 256000` to both makes the file match its own
documentation and its test contract. It is **not** a no-op:

```
provider_max = min(128000, 65536)   = 65536    # today, the omitted key fills DEFAULT_MAX_TOKENS
provider_max = min(128000, 256000)  = 128000   # after
```

`pack_cap` (`pipeline.py:1343`) and the `[info]` note follow, and every chapter
**re-packs**. A chapter mid-flight trips the packing-drift check at
`pipeline.py:1358-1364` and retranslates from scratch. That is the intended
direction — `EXAMPLES.md:178` shows `max` effort needs a large budget — but it is
a live behavior change to a shipped example and is recorded as one.

`[rev 2 — rev 1 claimed this "leaves provider_max at min(128000, 256000) =
128000, unchanged". Wrong: 128000 is the value AFTER, and today it is 65536.]`

Pre-existing defect, unrelated cause (`745433e` fixed the example
incompletely); fixed here because the suite must be green and the file is
already being edited. Flagged rather than absorbed silently.

## 8. Explicit non-goals

- **Not** making the failed consensus louder. The silent-degrade path and the
  4× retry on a permanent 400 are real defects (each costs a quality drop and
  three wasted calls), but both are separate behavioral changes with their own
  migration impact. Recorded here, not fixed here.
- **Not** touching `translate_max_output_tokens` semantics — v012 owns those.
- **Not** adding a per-job consensus ceiling. One provider block, one limit.
- **Not** fixing the truncated-chunk retry becoming a duplicate. With
  `provider_max = 131072` under a 256000 ceiling, `escalated` is 256000 and both
  the first attempt and the retry resolve to 131072 — an expensive no-op on a
  `max`-reasoning block. Pre-existing; recorded so §3.2's "correct for free" is
  not read as "and useful".

## 9. Verification

1. `uv run tests/run_all.py` green — meaning `test_sync_config.py`'s 3
   pre-existing `case_10` failures (§1.1) are cleared *and* no other script
   regresses. Baseline captured at `745433e`: 46 pass / 1 fail, 1960 checks.
2. New cases in `test_token_cap_ceiling.py`, which drives the **real**
   `client.chat` against a fake `requests.post` — the only place the clamp is
   observable, because `test_consensus.py`'s `FakeChat` replaces `client.chat`
   outright (its own `b8` comment says so).
3. A packing case: a translator block whose `max_tokens_limit` is below its
   `max_tokens` must lower `pack_cap` — asserted on `pack_cap` (or the chunk
   count) **directly**. `[rev 2 — rev 1 said "proven by the `[info]` note's
   number"; `_note_token_cap` returns early when `provider_max >= wire_cap`
   (`pipeline.py:810`), so under a 65536 ceiling that note never prints and the
   assertion would pass vacuously.]`
4. `v013` lines asserted in `test_migrate.py`, including idempotence and the
   byte-identical guarantee v012 established (`case_14`'s shape).
5. `test_migrate.py` chain-head assertions updated for the new head — this is
   **required, not optional**. `case_13` pins the head as a literal:

   ```python
   # test_migrate.py:1519-1523
   check("13k v012 is the chain head",
         migrations.chain()[-1].VERSION == 12
         and migrations.current_version() == 12, ...)
   ```

   Adding `v013.py` breaks `13k` until the literal becomes 13 and the check is
   renamed. Also required: import `v013` at `test_migrate.py:89`, add a
   `case_15_v013()` modelled on `case_14_v012` (`:1531-1610`), and register it
   in `main()` at `:1631`. `case_4_real_chain` (`:546-551`) derives from
   `current_version()` and needs no edit.

6. `references/file-formats.md` "Migrations" gets a `v013` sentence in the
   `v001…v012` narrative (`:889` onward) — the contract that section states is
   normative, so a shipped step missing from it is the same class of rot as a
   stale default.

### 9.1 New test cases the audit added

Beyond the matrix rows, four cases that no existing test covers:

- **Absence means unchanged, for a block that DOES declare `max_tokens`.**
  `test_token_cap_ceiling.py:243-272` only pins the *omitted*-key case, which is
  not the backward-compatibility claim being made here.
- **A translator's `max_tokens_limit` bounds its fan-out candidates too**, and
  does *not* disturb a block already below it. The audit asked for the opposite
  ("does not leak into the fan-out"); that was wrong against §6 decision 2 — the
  limit is a *provider* ceiling, so a candidate the provider would reject must
  be clamped. What must not happen is the limit becoming a second, lower budget
  for a block that never asked for more.
- **The synthesis floor survives a limit above it** — an under-provisioned
  arbitrator whose provider allows more than the task cap still gets the task
  cap (`j5`), and one whose provider allows less gets exactly that (`j6`).
- **`limit ≥ block max_tokens` is a no-op** (§5 row 9) — the branch where a user
  writing a limit would expect a raise and gets nothing.
- **The consensus block inherits `max_tokens_limit` from `translator[0]`** when
  it is not authored (`config.py:222-223` + `_with_defaults`), so the documented
  inheritance is pinned rather than assumed.