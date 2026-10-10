# PLAN: require `consensus` when a job actually fans out

**Status:** awaiting review. No code changes made yet.
**Decided by the maintainer**, answering a design fork:

> if any job has more than 1 provider and consensus is not defined, refuse to
> run. consensus should only block if we need it.
>
> we should just record what provider was used in general for what call

This replaces the case-2 `[warn]` shipped earlier today, and replaces the idea
of an explicit `consensus_fallback` key (considered and rejected — see §3).

**Baseline:** `uv run tests/run_all.py` -> 50 passed, 0 failed (50 scripts,
2351 checks).

---

## 1. Goal

Two things, at very different levels of cost.

**A. Refuse to run when a fan-out job has no explicit arbitrator.** Today, a
config that fans out without authoring `providers.consensus` silently merges
through a block derived from `providers.translator[0]` — verified key for key,
including its `temperature`, `max_tokens`, `max_tokens_limit` and `extra_body`.
The maintainer's ruling: that inference must not decide where a paid merge
call goes.

**B. Record which provider served which call.** **This is already done** — see
§2. The only gap is display.

---

## 2. Part B is mostly built; the gap is one missing column

`client._request_meta` / `_response_meta` (`lib/client.py:359-384`) already
write `call_id`, **`url`**, and `model` onto every `llm_request` /
`llm_response` row, and `_trace_hook` (`lib/consensus.py:77`) adds `job`,
`consensus_for`, `candidate`/`candidates` and `chapter`. The dashboard's call
ledger carries all of it forward:

```python
# lib/logdashboard.py:541-551
"job": src.get("job"),
"model": src.get("model"),
"url": src.get("url"),          # <-- collected
"candidate": src.get("candidate"),
"consensus_for": src.get("consensus_for"),
```

**But `url` is never rendered.** The ledger table's columns are
`call id | chapter | job | model | shape | tokens | reasoning | elapsed`
(`logdashboard.py:1730-1734`), and the filter placeholder reads "filter by
chapter, job, model or call id" (`logdashboard.py:1724`). The endpoint is in
the data, in the blob store, and nowhere on the page.

So Part B reduces to: **add an `endpoint` column to the call ledger and make it
filterable.** No new recording, no schema change, no log-format change — the
data is already there from the day the ledger shipped.

There is already a "Model roster" section (`_model_table`, `logdashboard.py:1475`)
answering "which model served which job" at a token-weighted level. The
endpoint is the missing dimension: two providers can serve the same model name,
and `model` alone cannot tell them apart.

---

## 3. Why no `consensus_fallback` key

The maintainer's first instinct was an explicit fallback provider. Rejected on
cost, not taste:

- It adds a **second** special key next to the one that already means "the
  arbitrator", where the complaint is that the current rule is hard to learn.
- It creates a **new inheritance question**: with `consensus_translator` and
  `consensus_fallback` but no `consensus`, what does the per-job block inherit?
  Every new layer needs a rule, and this one has no natural base.
- The proposed alternative satisfies the same need with **no new key**: the
  fallback is either explicit (`consensus` present) or the config does not run.
  There is no third state to reason about.

---

## 4. Part A -- the change

### 4.1 Where

`config._normalize_providers`, immediately after the existing
`consensus`-inherits-`translator[0]` branch. That function is the single
validation chokepoint: `load_config` already maps its `ValueError` to
`[FAIL] cannot read config.json: ...` and exit 2, and `apply_overlay` already
validates through it (`config.py:384`) before writing anything. So a `raise`
there IS "refuse to run", with no new plumbing.

### 4.2 The condition

```python
if "consensus" not in providers:            # raw dict: AUTHORED, not derived
    fanned = [job for job in PROVIDER_JOBS
              if job != "consensus" and len(normalized[job]) > 1]
    if fanned:
        raise ValueError(...)
```

Two details that are load-bearing:

- **Authorship is read from the raw `providers` argument**, not `normalized`.
  `_normalize_providers` always materializes `consensus`, deriving it from
  `translator[0]` when absent — so the normalized dict can never distinguish the
  two. The raw argument is the only honest source. (This is the same split
  `pipeline._note_consensus_routing` needed, which is why that function's
  finding is reusable here.)
- **`fanned` is computed on the normalized dict**, because omitted jobs inherit
  `translator`'s whole array: a 2-block translator means all six jobs fan out,
  so the message must name all six, not just the one the file spelled out.

### 4.3 The message

Names the offending jobs, the count, and the two ways out. It must not merely
complain, because the fix has two legitimate shapes:

```
providers.consensus is required because 2 job(s) fan out to more than one
model (annotator, translator) and no consensus provider is set. Add it
explicitly, or give those jobs a single provider block.
```

### 4.4 Blast radius: effectively zero, and verified

Scanned **every** `.json` in the repo for "some job has >1 block AND no authored
`consensus`" — **0 matches**:

| file | fans out? | `consensus` authored? |
|---|---|---|
| `config.local.example.zai.json` | no (all single-block) | yes |
| `config.local.example.minimax.json` | no | yes |
| `config.local.example.mixed.json` | yes (translator, annotator) | **yes** |
| `tests/review-e2e/config.json` | — | **yes** |

And `init` writes `providers` by iterating `PROVIDER_JOBS`
(`translate.py:309-320`), which **includes `consensus`** — so every
`init`-created project has it on disk from the start, and gets a fresh-project
stamp at the chain head that never migrates. The refusal can therefore only
reach a hand-written or pre-v8 config, which is exactly the population whose
merge routing is currently implicit.

**No lockout.** `apply_overlay` validates the MERGED config, so `sync-config`
with an overlay that *adds* `consensus` passes and repairs such a project; an
overlay that would leave it invalid is refused, which is correct since the
result could not load anyway.

### 4.5 Knock-on: the case-2 `[warn]` becomes unreachable

`pipeline._note_consensus_routing` fires exactly when `consensus` is not
authored. After §4.2 no such config can load, so **the function can never run
in production** and is dead code shipped today.

It is removed rather than kept as a fallback. Its test
(`test_cleanup_flow.case_consensus_routing_warn`) is deleted with it; the
behaviour it warned about is now a load error with a better message, so
nothing is lost but a line of console.

This is net code **reduction** despite the rule being added, which is the main
argument that "refuse" beats "warn" here.

---

## 5. Migration

**Fold into `v015`**, which is unreleased (untracked file, not in git), rather
than adding `v016`. One shipped step beats two.

`v015` already reads the raw config and already computes which jobs fan out, so
this is additive. It must now report the **blocking** condition, not just the
routing map:

```
[FAIL] config: providers.consensus is not set but 6 job(s) fan out to more
than one model (annotator, glossary, profile, recap, reviewer, translator).
As of v015 a run refuses to start until you add it explicitly, or give those
jobs a single provider block. Nothing was rewritten.
```

`[FAIL]`, not `[warn]`, because the migration predicts a run that will not
start; `[warn]` would understate it. The rest of `v015`'s report is unchanged.

**The migration does not rewrite either.** It cannot invent a model choice for
the operator, and auto-writing `consensus` from `translator[0]` would
re-create the very inference this change forbids — silently, on disk, with no
line to explain it.

---

## 6. Tests

### 6.1 Audit finding: the on-disk scan understates the blast radius

The §4.4 scan read every `.json` **file** and found 0 matches — which is
accurate for shipped configs but dangerously misleading, because most of the
suite builds its provider dicts **inline in Python** and feeds them straight to
`_normalize_providers`. A file-based scan cannot see those. At least four
existing cases in `tests/test_provider_arrays.py` use a 2-block translator with
no authored `consensus`, and every one of them would newly raise:

| case | fixture | what it currently pins |
|---|---|---|
| `case_list_passthrough:127` | 2-block translator, no consensus | array passthrough element-wise |
| `case_inheritance:147` | 2-block translator, no consensus | **omitted jobs inherit translator's array** |
| `case_load_config:265` | on-disk 2-block translator, no consensus | a real load survives an authored array |
| `case_per_job_consensus:289` | 2-block translator, no consensus | **absent-key identity with the derived global** |

The third row is the one that matters. `case_inheritance` is the executable
spec for "omitted jobs inherit the translator's whole array" — the rule that
decides how many jobs fan out, and therefore the rule this new refusal is
counting. It cannot simply be given a `consensus` key: that would stop testing
the inheritance it exists to pin.

**Resolution:** split it. Omitted-job inheritance is asserted with `consensus`
authored (still valid, still the rule that decides the fan-out set). The
second half — `consensus` taking `translator[0]` when unauthored — moves to its
own case that asserts the inheritance **still resolves**, and documents that
`load_config` can no longer reach it (§6.3). Behaviour preserved, coverage
made honest.

### 6.2 What the refusal makes unreachable

Two things, not one. Both were found by running the suite with the check in
place rather than by reading:

1. **`pipeline._note_consensus_routing`** — fires only when `consensus` is
   unauthored, which now cannot load. Deleted with its test (§4.5).
2. **The `consensus` <- `translator[0]` inheritance itself.** Every path to it
   is now closed: a config that fans out must author `consensus`, and a config
   that does not fan out never reaches an arbitrator. It is kept, not removed —
   `consensus_provider`'s absent-key path still calls `provider(cfg,
   "consensus")`, and a hand-built cfg bypasses `load_config` entirely, so
   deleting it would trade a harmless fallback for a `KeyError`. What changes is
   that it is documented as **not reachable through `load_config`**, so nobody
   again reads it as a live routing rule.

### 6.3 New / changed tests

**`tests/test_provider_arrays.py`** (in `case_per_job_consensus`):
- a 2-block translator + no authored `consensus` -> raises, message names the
  fan-out jobs and both remedies
- a 1-block translator (nothing fans out) + no `consensus` -> loads fine (the
  "only block if we need it" half)
- fan-out confined to one job -> the message names only that job
- `consensus` authored -> loads, whatever the fan-out
- `consensus_translator` present and no `consensus` -> **still raises**: a
  per-job key does not substitute for the global, which is the rule this change
  exists to make unmissable
- the identity assertion keeps its dict-identity shape but is re-pointed at an
  **authored** global, since a derived one is no longer reachable

**`tests/test_migrate.py`** (`case_17_v015`): the fan-out-without-consensus
fixture asserts `[FAIL]`, and the report names the jobs.

**`tests/test_cleanup_flow.py`**: `case_consensus_routing_warn` deleted with
the function. No replacement — the condition is unreachable.

**`tests/test_sync_config.py`**: an overlay that adds `consensus` to a
fan-out-without-consensus project applies cleanly — the repair path, and the
one that proves no lockout.

---

## 7. Docs to update

- `references/file-formats.md`: add the refusal to the error-message contract
  beside the other four `providers` ValueErrors; **delete** the case-2 `[warn]`
  paragraph added earlier today; update the v015 Migrations entry to `[FAIL]`.
- `SKILL.md`: same — drop the third `[warn]` bullet, restore "Two `[warn]`
  lines".
- `README.md`: `providers` bullet gains "if any job fans out, `consensus` is
  required".
- `config.local.EXAMPLES.md`: the "Write the global block first" advice is now
  enforced rather than advised — keep it, reworded as a requirement.
- No `assets/templates/` change; no log-format change.

---

## 8. Part B implementation (display only)

`lib/logdashboard.py`:
- add an `endpoint` column to `_call_ledger`'s table, rendering
  `_esc(call["url"])` (trim the `/chat/completions` suffix so a column of
  identical repeated paths is readable — the endpoint, not the operation)
- extend the filter's match predicate to include it, and its placeholder text
- no change to `_ledger_js`, the blob store, or the JSONL schema: `url` is
  already carried in `calls[]` and already reaches the client-side store

Tests: `tests/test_log_dashboard.py` gains a case asserting the endpoint column
appears for a call whose `url` is set, and that the filter matches on it.

---

## 9. Acceptance criteria

1. `uv run tests/run_all.py` green, checks >= 2351 minus the deleted warn-case
   (~12), i.e. the net must not drop the suite below a clean pass.
2. A config that fans out without `consensus` fails to load with a message
   naming the jobs:
   ```
   rg -n 'providers.consensus is required' novel-translator/scripts/lib/config.py
   ```
3. A config that fans out WITH `consensus` loads, and `consensus_provider`
   resolution is unchanged (existing case (b2) dict-identity check still holds).
4. A single-block config without `consensus` loads.
5. `git diff` shows `_note_consensus_routing` gone from `lib/pipeline.py` and
   the dashboard's `calls[]` dict untouched apart from rendering.

---

## 10. Risks and open questions

- **This breaks hand-written configs that fan out without `consensus`.** That
  is the point, but it is a hard stop, not a warning — worth being explicit
  that a maintainer mid-run will see exit 2, not a degraded chapter.
- **`ping` will also refuse.** It loads the config first. For someone trying to
  *diagnose* the very config that just failed to load, that is unfortunate.
  Rejected for now because it matches every other provider validation, but
  say if you would rather `ping` report-and-continue on this one condition.
- **Is `consensus` required, or is an explicit per-job arbitrator enough?** The
  plan refuses even when every fanning-out job has its own `consensus_<job>`.
  The alternative — allow it when no job actually falls back to the global —
  is defensible and is a one-line change to the condition. Flagged because it
  is the one place where "refuse" could be narrower than you intended.