# AUDIT — `PLAN-2026-10-07-consensus-cap.md`

Date: 2026-10-07
Plan audited: `PLAN-2026-10-07-consensus-cap.md` (rev 1)
Auditor: `verifier` subagent (adversarial, read-only) + maintainer's own code read.
Verdict: **REWORK** — the mechanism is sound, two of the plan's own claims are arithmetically false.

Repo at `745433e`. Unmodified during the audit. No files were changed by either pass.

---

## How to read this document

Findings are split by what they actually changed:

- **Stale** — the finding is correct about the repo, but the plan had **already been
  corrected** before the auditor read it. Kept because they document *why* the
  plan says what it says, and because two of them contain reasoning worth keeping.
- **New** — a genuine error in the plan that the audit introduced. Each one is
  folded into the plan before implementation.

A finding is marked STALE only after checking the plan text myself. The auditor
read the plan while it was being revised; several of its "blockers" cite line
numbers that rev 1 had already moved.

---

## BLOCKER 1 — the baseline is red *(STALE: fixed in rev 1 §1.1 + §4 item 5)*

The auditor measured `uv run tests/run_all.py` on the untouched repo:

```
46 passed, 1 failed (47 script(s), 1960 check(s))
uv run tests/test_sync_config.py -> 100 passed, 3 failed
```

all three on `config.local.example.mixed.json`. Independently confirmed, and
already written up in plan §1.1 before this finding arrived.

The auditor adds one detail the plan had missed: **the `annotator[1]` MiniMax
block (`config.local.example.mixed.json:49-59`) fails identically** to the
translator block — same missing `max_tokens`, same `reasoning_effort: max`. A
fix scoped to the translator block alone would clear one of three failures.

Rev 1 §4 item 5 already says **"both MiniMax blocks"**, and §1.1 already quotes
`['annotator[MiniMax-M3.1-Flash-Preview]', 'translator[MiniMax-M3.1-Flash-Preview]']`.
**No plan change needed**; the finding is kept because it independently confirms
the cause (`block.get("max_tokens", 0)` reading the RAW overlay, not the
normalized config — `test_sync_config.py:791-793, 800-802, 816-821`).

## BLOCKER 2 — §7.1's arithmetic is false *(NEW — folded into the plan)*

> Plan claimed: *"leaves `provider_max` at `min(128000, 256000) = 128000`,
> unchanged."*

**Wrong.** Today the MiniMax translator block has no `max_tokens`, so
`config.py:135` fills `DEFAULT_MAX_TOKENS = 65536` into it via `_with_defaults`
(`config.py:183-188`) and `pipeline.py:1338-1341` computes:

```
provider_max = min(128000, 65536) = 65536        # today
provider_max = min(128000, 256000) = 128000      # after adding max_tokens: 256000
```

`pack_cap` follows (`pipeline.py:1343`), the `[info]` note's number follows
(`pipeline.py:813-816`), and every chapter **re-packs**. A chapter mid-flight
hits the packing-drift check at `pipeline.py:1358-1364` and retranslates from
scratch.

This is the same class of mistake as the matrix error below — I checked the
value *after* the edit and called it unchanged, without checking the value
*before* it. Corrected in plan §7.1, and the correction is stated as a real
behavior change to a shipped example rather than a no-op.

## MAJOR 1 — §4 item 4 cited the wrong line *(STALE: fixed in rev 1 §3.1b)*

The auditor is right that the fix belongs at `pipeline.py:836`, not `:841`:
editing only `:841` leaves `block_max` raw while packing (`:1339`) uses
`block_cap`, so a block at `max_tokens: 65536, max_tokens_limit: 4096` packs at
4096 with **no warning** and fragments the chapter — the condition
`pipeline.py:822-824` calls "a hard error, not a tuning choice".

Rev 1 §3.1 and §3.1b had already moved the edit to `:836` and worked through
the consequence. **No further change**; the reachable bad state the auditor
names is the same one §3.1 uses as its example.

## MAJOR 2 — §5 rows 4 and 5 are arithmetically wrong *(NEW — folded into the plan)*

> Plan claimed, for a **synthesis** call with block `max_tokens: 65536` and task
> cap 4096: sent **4096**.

**Wrong — it sends 65536.** `consensus.chat` computes the floor *before*
`client.chat` ever sees the value:

```python
# consensus.py:214
c_max = max(max_tokens or 0, int(cblock.get("max_tokens") or 0)) or None
```

`max(4096, 65536) = 65536`. My table treated "task cap" as the value reaching
`_resolve_cap`, which is false on every synthesis row — the value that arrives is
already `max(task, block)`.

The auditor's supporting point is the right correction to the fixture reading:
`test_consensus.py:288` (cblock 2048 vs task 1000) and `:302` (cblock 512 vs
task 4096) only exercise the shape where **the block is below the task cap** —
the one case where the two readings coincide. A test written from my broken row
would have failed.

The matrix is rebuilt in plan §5 with an explicit `c_max` column.

## MAJOR 3 — v013's detector has the wrong polarity *(NEW — folded into the plan)*

> Plan proposed: report when "consensus declares N, the translator ceiling is
> M > N, no limit declared".

**Both directions are wrong.**

*False negative.* The trigger should be `max(task_cap, cblock.max_tokens) >
provider_hard_limit`. A stock project has `consensus.max_tokens = 65536`
(`config.py:146`) and `translate_max_output_tokens = 65536`, so `M > N` is
false and v013 stays silent — on the configuration where the synthesis is the
thing most likely to be sent above the block's own number.

*False positive.* Since v012, `translate_max_output_tokens > consensus.max_tokens`
is an explicitly **supported** configuration (`file-formats.md:313-317`). A
project with ceiling 128000 and consensus 65536 works correctly against a
131072-limited provider, and v013 would warn about it.

The plan also contained a nonsense sentence — *"§6 of the migration contract is
satisfied by the report itself"* — which referred to its own decision list
rather than to the contract. Removed.

**Folded in:** v013's trigger becomes *"the synthesis will send more than the
consensus block's own declared `max_tokens`, and no limit is declared"* — the
condition that is factually observable without knowing the provider. It reports
as `[info]`, never claims the provider will reject, and says explicitly that the
shape is supported.

## MINOR 1 — the `extra_body` claim is wrong for the key the examples actually use *(NEW — folded in)*

> Plan claimed: *"`extra_body` still wins … it merges after everything
> (`client.py:218-222`)."*

`body.update(extra)` can only override a key that is literally present.
`extra_body` in every MiniMax block of the shipped examples carries
`max_completion_tokens` (`config.local.example.mixed.json:19,56`) — a **different
parameter**, added *alongside* `max_tokens`, not instead of it. Pinned at
`test_token_cap_ceiling.py:173-179` (`body["max_tokens"] == 128000 and
body["max_completion_tokens"] == 65536`).

So `max_tokens_limit` bounds the `max_tokens` field only; a provider that
honors `max_completion_tokens` is outside the ceiling entirely. Plan §3.1c and
§6.5 corrected, and the limitation is stated in the doc key table rather than
implied away.

## MINOR 2 — "both call sites" is false, and `client.py:144` names a function that does not exist *(NEW — folded in)*

The plan wrote *"Both call sites use it, so they cannot drift."* There are four
runtime readers of a block's cap:

| site | role | plan rev 1 |
|---|---|---|
| `client.py:147` | the sent value | fixed |
| `pipeline.py:1339` | packing minimum | fixed |
| `pipeline.py:836` | the 8192 floor + squeeze check | fixed |
| `consensus.py:214` | the synthesis **floor** | deliberately unchanged, correctly |

Also: `provider_max` is a local variable inside the TRANSLATE loop
(`pipeline.py:1338`), not `pipeline._provider_max`. `client.py:144`'s docstring
already misnames it that way; corrected in the same change since the plan edits
that exact docstring.

## MINOR 3 — the proposed packing assertion would silently never fire *(NEW — folded in)*

> Plan §9.3: *"proven by the `[info]` note's number."*

`_note_token_cap` returns early when `provider_max >= wire_cap`
(`pipeline.py:810`). With a limit of 131072 under a 65536 ceiling the note never
prints, so the assertion would pass vacuously. Verification now asserts
`pack_cap` (or the chunk count) directly, and the note only where it does print.

## MINOR 4 — two test gaps *(NEW — folded in)*

(a) No case pins **absence-means-unchanged** for a block that *does* declare
`max_tokens` — `test_token_cap_ceiling.py:243-272` covers only the *omitted*-key
case, which is not the actual backward-compatibility claim being made.

(b) No case pins that a `max_tokens_limit` on a **translator** block reaches
the fan-out candidates. **Rejected as stated, and this is the one audit finding
the implementation overrules.** The finding assumed the limit is a
synthesis-only rule; §6 decision 2 makes it a *provider* ceiling, which by
definition bounds every call — a candidate the provider would reject must be
clamped. What the tests pin instead (`j4`) is that the limit bounds an
over-reaching block **and** leaves a block already below it untouched (`j3`), so
it cannot become a second, lower budget.

## MINOR 5 — the consensus job inherits the key from `translator[0]` *(NEW — folded in)*

`config.py:222-223` — `authored = [translator[0]]` — and `_with_defaults` copies
authored keys verbatim. So a `max_tokens_limit` on the translator's first block
silently bounds a `consensus` block that may point at a completely different
provider. This is exactly how `max_tokens` already behaves, so it is not a
defect — but consensus is the one job whose cap is otherwise reinterpreted, so
the key-table entry says so.

## MISSED — four items the plan did not ask about

1. **The baseline is red.** Nobody checked the plan's baseline claim. Fixed in
   §1.1 (independently, before the audit returned).
2. **`annotator[1]` needs the identical fix.** Covered by rev 1 §4 item 5.
3. **`config.local.EXAMPLES.md:209-214` carries the same false claim** the plan
   was corrected on: *"`pack_cap` is `min(ceiling, smallest translator block)` … about 200k at 256k."* Today that is `min(256000, 65536) = 65536`, ~52k — as the same paragraph concedes one sentence earlier. Added to the doc scope.
4. **The "limit ≥ block max" direction had no matrix row.** `max_tokens: 65536,
   max_tokens_limit: 131072` must still send 65536. This is the mistake a user
   is most likely to make — writing a limit believing it raises the block — and
   the one branch where `min()` does nothing at all. Row added.
5. **Under a limit the truncated-chunk retry becomes a pure duplicate.** With
   `provider_max = 131072` under a 256000 ceiling, `escalated` is 256000 and both
   the first attempt and the retry resolve to 131072. On a `max`-reasoning block
   (5–10 min/call, `EXAMPLES.md:178`) that is an expensive no-op. Pre-existing
   and out of scope; recorded so "correct for free" is not read as "and useful".

---

## What survived

The auditor confirmed, against the code:

- **The diagnosis.** `consensus.py:214`'s `max()` plus `enforce_ceiling=False`
  is exactly what sent 256000 past a 131072 provider limit.
- **The mechanism.** `block_cap` in `_resolve_cap` is safe in every branch —
  falsy/`None` caps, `enforce_ceiling` both ways, and a limit ≥ the block's max.
  `_resolve_cap` is a genuine choke point: every `max_tokens=` in the repo
  funnels `pipeline._chat` → `consensus.chat` → `client.chat` → `_resolve_cap`.
- **§3.2's "`_escalated_cap` is correct for free."** `provider_max` carries the
  limit, and the retry stays clamped at the wire.
- **`consensus.py:214` should not change.** Its `max()` is the feature.
- **`test_token_cap_ceiling.py` is the right home for the new tests** — it drives
  real `client.chat` against a fake `requests.post`, which is the only place the
  clamp is observable (`test_consensus.py`'s `FakeChat` replaces `client.chat`
  outright, as its own `b8` comment notes).
- **The existing pins survive.** `b4`/`b7`/`b8`/`b9` and all of
  `test_token_cap_ceiling.py` are unaffected by the plan as written.
- **Report-only `v013` is compliant** with `AGENTS.md` and with
  `migrations/__init__.py:46-64`.
- **Keep `ValueError` on a non-numeric `max_tokens_limit`** — parity with
  `max_tokens` (`client.py:147`) and with `file-formats.md:301-306`, which
  excludes provider sampling knobs from validation. This closes the plan's one
  open question.
- **Every line number cited in the plan's §1, §2 and §7.1 checks out.**

---

## Folded into the plan before implementation

1. §7.1 rewritten: the delta is `provider_max` **65536 → 128000**, chapters
   re-pack, and it is a live change to a shipped example.
2. §5 rebuilt around an explicit `c_max` column; rows 4/5 corrected, the
   limit-≥-block no-op row added.
3. §3.1c and §6.5 corrected: the ceiling bounds `max_tokens` only;
   `max_completion_tokens` is outside it.
4. v013's trigger replaced with the synthesis-exceeds-block condition, `[info]`
   severity, and an explicit "this shape is supported" sentence; the nonsense
   §6 cross-reference deleted.
5. §4 item 4 corrected to cite `pipeline.py:836` as the edit site and to name
   all four cap readers.
6. §9.3 verification switched from the `[info]` note to `pack_cap` directly.
7. Test scope extended: absence-means-unchanged with a declared `max_tokens`, and
   no-leak-into-fan-out.
8. Doc scope extended with `EXAMPLES.md:209-214`.
9. `client.py:144`'s stale `pipeline._provider_max` reference corrected.