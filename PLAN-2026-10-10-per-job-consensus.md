# PLAN: per-job consensus providers

**Status:** IMPLEMENTED. Audited in three rounds
(`AUDIT-2026-10-10-per-job-consensus.md`); every round came back with no
blocker, and all findings are closed in the plan text and in the code.
Result: 50 passed, 0 failed (50 scripts, 2339 checks), from the baseline
49 scripts / 2280 checks. Deviations from the plan as written are recorded in
audit round 3.

Two design points to keep in mind when reading the rest of this document,
because they are what the design rests on:

- **The plan's §3.1 resolution rule shipped unchanged.** Every finding the
  audit raised was a specification gap, never a reason to move the design.
- **The three open questions in §12 were resolved by implementation**, using
  the plan's own recommended option each time: `consensus_<job>` for naming,
  raise on an unknown suffix, and keep the `ping` addition. Each is one line to
  flip if you disagree; none is load-bearing for the rest.

**Baseline:** `uv run tests/run_all.py` -> 49 passed, 0 failed (49 scripts, 2280 checks).
**Requested by:** the maintainer -- "I want to split the consensus calls into different
consensus providers. for example, I want to use different consensus models for
translations, annotations etc."

---

## 1. Goal

Let each fan-out job name its own arbitrator, so the model that merges
`translator` candidates can differ from the one that merges `annotator`
candidates.

Today there is exactly one arbitrator for the whole pipeline. Every merge --
translator, annotator, glossary, recap, profile, reviewer -- goes to
`providers.consensus`, resolved at `lib/consensus.py:231`:

```python
cblock = config.provider(cfg, "consensus")
```

The cost of that coupling is already written down in the maintainer's own
routing table (`config.local.EXAMPLES.md:326, 337-338`):

> | `consensus` | `glm-5.3` | synthesizes every fan-out, so it gets the strongest |
> ... Keep `consensus` on the flagship if translator quality is the priority: it
> is the synthesizer for *every* fan-out job, so demoting it to save credits
> would degrade the translator too.

That is exactly the lever the maintainer wants: a big-context model for the
translation merge, a cheap fast model for the annotation merge, without
demoting either.

**Success condition:** a project can set `providers.consensus_translator` and
`providers.consensus_annotator` to different models, and every existing project
that sets neither behaves byte-identically to today.

---

## 2. Non-goals

| Item | Why it stays out |
|---|---|
| A *different number of candidates* per job | Already possible -- it is just the array length of `providers.<job>`. Unrelated to who merges. |
| Nested `providers.consensus.translator` shape | Rejected in Step 0 below. |
| Logging changes | The trace already records the arbitrator's resolved `model` on the consensus call (`lib/consensus.py:268`), and the dashboard already renders it as the `consensus -> {task}` column header (`lib/logdashboard.py:1892`). The routing is observable without a new field. |
| Changing the failure policy | Terminal on failure, no survivor fallback (`lib/consensus.py:16-25`). Untouched. |
| Packing the chapter against the arbitrator's cap | See §9 -- a pre-existing asymmetry, not introduced here and not fixed here. |
| `v013`'s report reading a per-job block | Migrations are version-ordered; a v13 config cannot contain a v15-era key. See §7. |

---

## 3. Step 0 -- the shape decision, and why the obvious alternative is out

There are three places to put the per-job arbitrator. Two are rejected on
evidence, not taste.

**Rejected: nested under the existing key** (`providers.consensus.translator`).
`providers.consensus` must today be a provider block *or* an array of blocks
(`config._provider_blocks`, `config.py:161-180`), and a provider block **is a
dict**. So `{"translator": {...}, "default": {...}}` is indistinguishable from
a provider block that happens to carry two unexpected keys. `_provider_blocks`
would wrap it into a one-element list, `client.chat` would send a block with no
`base_url`, and the missing endpoint would fall back to the hard-coded
`DEFAULT_BASE_URL` (`config.py:97`) -- silently pointing the merge at a
different server. Distinguishing the two shapes would require sniffing for
known job names, which is precisely the heuristic
`references/file-formats.md:332-336` promises will never be needed. Out.

**Rejected: a new top-level `consensus_overrides` key.** It would not be
ambiguous, but it splits provider configuration across two places and needs its
own normalizer, inheritance rule and default-filler -- all reimplementing what
`_normalize_providers` and `_with_defaults` already do. Out.

**Chosen: sibling keys `providers.consensus_<job>`.** The `providers` namespace
is already flat and job-keyed, `_provider_blocks` /
`_with_defaults` already handle the shape, and a new key sorts next to
`consensus` in the JSON file.

### 3.1 The resolution rule

Three layers, bottom-up. Each layer fills only the keys the layer above left
unset -- the same per-key semantics as `_with_defaults` (`config.py:183-188`):

```
providers.consensus_<job>  =  PROVIDER_DEFAULTS["consensus"]      (base)
                           <- resolved providers["consensus"]     (global arbitrator)
                           <- authored providers.consensus_<job>  (wins)
```

If `providers.consensus_<job>` is **absent**, the arbitrator *is* the global
`consensus` block, resolved exactly as today. That is the whole
backward-compatibility story: a project with no `consensus_*` key takes the
identical code path it takes now.

Two consequences worth stating explicitly, because both are deliberate:

1. **A partial block inherits the global arbitrator's endpoint and auth.** A
   user who writes `{"model": "big-arbiter"}` and nothing else keeps their
   working `base_url`/`api_key_env`. Filling from `PROVIDER_DEFAULTS` instead
   would re-point them at the hard-coded `DEFAULT_BASE_URL` -- the exact failure
   `merge_overlay` was written to prevent (`config.py:328`: "A partial overlay
   must never quietly un-point a working provider"). The resolution therefore
   layers onto the **resolved** global, not onto the defaults.

2. **Inheriting `consensus`, not `translator[0]`.** The global already inherits
   `translator[0]`'s authored keys when it is itself absent
   (`config.py:221-223`), so the stack is monotone: the more the user fills in,
   the less is inherited.

### 3.2 Where the layering lives: read time, not load time

`config.consensus_provider(cfg, job)` does the full layering **at call time**.
`_normalize_providers` only validates the shape of an authored
`consensus_<job>` key -- it does **not** pre-fill it.

Reason: the test suite builds cfg dicts by hand and never passes them through
`load_config` (stated outright in `tests/test_consensus.py:42-43`: "cfg dicts
are hand-built (never load_config'd)"). Resolving only at load time would make
the hand-built cfgs -- which are the executable spec for `consensus.chat` --
resolve differently from real ones. Resolving at read time makes the semantics
true for both, so a test can assert the real behaviour without a temp project.

The deliberate cost: in a *loaded* cfg, every other job's blocks are fully
default-filled but a `consensus_<job>` block is not. That asymmetry is safe
because `consensus_<job>` is read through exactly one function,
`consensus_provider`, which returns a complete block. Nothing else reads it.
The docstring will say so.

---

## 4. Step 1 -- `config.py`

### 4.1 New public function

```python
CONSENSUS_PREFIX = "consensus_"

def consensus_provider(cfg: dict, job: str) -> dict:
    """The single arbitrator for `job`'s merge ..."""
```

- `key = CONSENSUS_PREFIX + job`
- `authored = cfg["providers"].get(key)`; if `None`, return
  `provider(cfg, "consensus")` **unchanged** -- the same dict object, not a copy.
  (`tests/test_consensus.py` case (b2) asserts `c["block"] is cblock`, so
  rebuilding the block here would break identity for every existing project.)
- Otherwise: `base = provider(cfg, "consensus")`; layer
  `PROVIDER_DEFAULTS["consensus"] <- base <- _provider_blocks(cfg["providers"], key)[0]`;
  return it.

Read the optional key with **`_provider_blocks`**, not `provider_list`:
`_provider_blocks` wraps a bare dict, validates a list (non-empty, every element
an object) and raises the three contractual messages with the key name
interpolated (`config.py:173-179`), so a malformed `consensus_translator` says
`providers.consensus_translator must be a provider block (object) or an array
of blocks` rather than failing as an `IndexError`. It also keeps this function
correct on the hand-built cfgs that never pass through `load_config` and so
were never validated by §4.2.

If `cfg` has no `consensus` key at all, `provider()` raises `KeyError` exactly
as it does today. That is the right failure: `load_config` guarantees the key,
so this is reachable only from a hand-built cfg, where falling back to
`PROVIDER_DEFAULTS` would silently aim the merge at the hard-coded
`DEFAULT_BASE_URL` instead of failing loudly.
- Call `_provider_blocks` for the authored value so the three contractual
  ValueErrors (`config.py:173-179`) apply verbatim, with the key name in the
  message instead of a hard-coded `{job}`.

### 4.2 `_normalize_providers` -- validate, don't fill

After the existing `PROVIDER_JOBS` loop, one extra pass over the authored
`providers` keys:

- Every key starting with `CONSENSUS_PREFIX` whose suffix is **not** a member
  of `PROVIDER_JOBS` raises:
  `providers.consensus_<suffix> is not a job (expected one of: translator, glossary, reviewer, annotator, recap, profile)`.
- A recognized suffix is passed through `_provider_blocks` (dict -> one-element
  list) and the same exactly-one check `consensus` gets, with its own key name
  in the message. Keys are **not** pre-filled -- that is §3.2.

**Normalize the shape to a list; do not pre-fill the keys.** The shape half is
load-bearing, not hygiene: the `run_start` event snapshots
`provider_jobs` by filtering `isinstance(blocks, list)`
(`translate.py:81-83`), so a bare-dict `consensus_translator` would vanish from
the run ledger -- the run would stop recording which model arbitrated the
translator, which is the first thing an operator reads after a fan-out goes
wrong. Normalized, the ledger picks it up for free, at run level, with no
log-schema change.

`consensus` is excluded from the valid suffixes (it can never hold two blocks,
so `consensus_consensus` would be accepted, normalized, and never read by
anything).

`PROVIDER_JOBS` and `PROVIDER_DEFAULTS` are **unchanged**. This is load-bearing:

- `tests/test_provider_arrays.py:83` asserts `PROVIDER_JOBS[-1] == "consensus"`,
  and `:110` asserts the normalized dict's keys are exactly `PROVIDER_JOBS`.
  Registering the new keys as jobs would break both and force `init` to write
  six dead blocks into every fresh config.
- An absent `consensus_<job>` stays absent, so a loaded config with none
  authored has exactly today's key set.

### 4.3 Why the unknown-suffix check is not a speculative guard

`_normalize_providers` passes unknown keys through untouched
(`config.py:233-235`). So `consensus_translatior` would load cleanly, never be
read by anything, and silently leave the translator merging through the global
arbitrator. That is reachable by a single-character typo on the exact key this
change introduces, and the consequence is a silent no-op on a quality-critical
setting -- the same class of bug as `075269f` ("warn instead of silently
ignoring"). The check is cheap and names the valid suffixes.

The existing `consensus` exactly-one message (`config.py:230-231`) is asserted
verbatim by `tests/test_consensus.py` case (h) and restated at
`references/file-formats.md:336`. It must stay byte-identical. The new keys get
the same message with their own key name substituted.

### 4.4 `merge_overlay` / `sync-config` -- no change needed

Checked, not assumed (`config.py:332-340`): an overlay naming
`consensus_<job>` follows the same two paths as any other job. Absent in the
project -> `_deep_merge` adds it wholesale; present -> the single-dict form
merges into every block, which is the "must never un-point" behaviour. Either
way `apply_overlay`'s validation call (`config.py:384`) runs
`_normalize_providers`, so a two-block `consensus_translator` in an overlay is
rejected before anything is written -- matching what
`references/file-formats.md:386-387` already promises.

---

## 5. Step 2 -- `consensus.py`

One line changes:

```python
- cblock = config.provider(cfg, "consensus")
+ cblock = config.consensus_provider(cfg, job)
```

Everything else in `chat()` is deliberately untouched:

- `c_max = max(max_tokens or 0, int(cblock.get("max_tokens") or 0)) or None`
  (`consensus.py:257`) now floors against the *per-job* block. Correct and
  automatic -- the whole point of v013's floor is that it must track whichever
  block is in force.
- `enforce_ceiling=False` unchanged; `max_tokens_limit` still clamps via
  `client._resolve_cap`.
- Trace tags stay `job="consensus"`, `consensus_for=<task>`
  (`consensus.py:268-269`). `logdashboard.key_task` groups on
  `consensus_for or job` (`logdashboard.py:1585`), so the fan-out still renders
  as one side-by-side matrix with one `consensus` column, and that column's
  header already names the arbitrator's model.
- The `[consensus] {job}: {n} model(s) - merging results via the consensus
  provider` line and its `_ANNOUNCED` dedup are unchanged (the string is
  asserted verbatim at `tests/test_consensus.py:86`).
- The lazy `from lib import pipeline` at `consensus.py:230` stays; that is the
  consolidation plan's problem, not this one's.

---

## 6. Step 3 -- `ping` covers the arbitrator that will actually run

`cmd_ping` (`translate.py:622-676`) iterates `PROVIDER_JOBS` and probes each
job's blocks. It would **not** probe a `consensus_<job>` block, because that key
is deliberately not a job. So the maintainer could point
`consensus_translator` at a dead endpoint and `ping` would report all green,
then a real chapter run would die fatally at the merge -- after the candidates
were already paid for.

Add a pass after the existing loop: for every job whose array has more than one
block, resolve `consensus_provider(cfg, job)`; if that block was not already
probed, probe it under the label `consensus({job})`.

**This closes a hole this change would otherwise open.** Today `consensus` is
in `PROVIDER_JOBS`, so `ping` probes it as a job. Making per-job arbitrators
deliberately *not* jobs removes them from that loop -- so without this pass the
one block whose failure kills the run after the candidates were already paid
for would be the only provider in the file `ping` cannot check.

Implementation notes that are easy to get wrong:

- The dedupe set must be **global across the whole probe**, not the per-job
  `seen` dict at `translate.py:628` -- that one is re-created every iteration
  of the job loop and is empty at the end of each. Build a separate set and
  add every block the main loop probed to it.
- Compare the block's own `(base_url, model, api_key_env, api_key)`, not the
  resolved model: the main loop resolves `model: null` through `/models`
  (`translate.py:636`), but two blocks identical in all four fields are the
  same provider either way.
- A failing arbitrator probe must set `failed = True` and print
  `[FAIL] consensus({job}) ...`, using the existing `_ping_err` helper, so it
  lands on the same exit 2 path as any other unreachable provider.

**Back-compat:** with no `consensus_*` keys authored, every job resolves to the
global `consensus` block, which the main loop already probed as job
`consensus` -- so the dedupe suppresses every extra line and `ping` output is
byte-identical for every existing project. That is the acceptance test.

---

## 7. Migration `v015.py`

Required by the AGENTS.md rule on **precedent, not letter**: no `DEFAULTS` or
`PROVIDER_DEFAULTS` entry is added, removed, renamed or re-keyed, and no
template changes, so the rule does not literally fire. `v009` set the precedent
of shipping an upgrade record for a change in MEANING; `v012`/`v013` for a
report-only step. This is a meaning change to the `providers` schema, so it
ships.

**Report-only, like `v012`/`v013`: nothing on disk is wrong, so nothing is
rewritten.** There is deliberately **no materialization** -- see §9.

Content: for each job that actually fans out (>1 block), report which
arbitrator is in force -- `providers.consensus` or `providers.consensus_<job>`
-- and apply `v013`'s check to the effective arbitrator: if the synthesis would
be sent more than that block's own `max_tokens` and the block declares no
`max_tokens_limit`, say so with the numbers.

**`v013` is left alone.** It reads the raw `providers.consensus`
(`v013.py:102`). A v13 config cannot contain a v15 key unless the maintainer
hand-wrote one before upgrading, and migrations never re-run once stamped, so
the imprecision is unreachable in practice. `v015`'s own report is the
authoritative one and will say which block it is describing.

---

## 8. Tests

Baseline 2280 checks; the count must not drop.

**`tests/test_provider_arrays.py`** (config resolution, against
`_normalize_providers` and `consensus_provider` directly):
- absent `consensus_<job>` -> arbitrator `is` the global block, identical dict
- authored `{"model": "m"}` -> new model, **inherited** `base_url`/`auth`
- authored full block -> wins over the global key-wise, global fills the rest
- global itself absent (inherited from `translator[0]`) -> layering still works
- two-block `consensus_<job>` -> exact ValueError naming that key
- unknown suffix `consensus_trans` -> ValueError listing the valid suffixes
- every `PROVIDER_JOBS` member is accepted as a suffix
- `PROVIDER_JOBS` and `PROVIDER_DEFAULTS` are unchanged in shape/size

**`tests/test_consensus.py`** (the routing itself):
- a two-block `annotator` job with `consensus_translator` set merges through the
  **translator's** arbitrator and NOT its own -> the fake records the block's
  model, proving per-job routing rather than per-job coincidence
- the existing suite's cfgs (no `consensus_*` key) keep resolving to the global
  block -- the existing cases (b2: `c["block"] is cblock`) already prove this

**`tests/test_sync_config.py`**: a `consensus_<job>` in a `config.local.json`
overlay applies to a project that has the key and is added to one that does
not; a two-block overlay array is rejected before any write.

**`ping` -- NEW test surface.** There is no ping harness today:
`rg 'cmd_ping|_ping_err' tests/` returns nothing, and `cmd_ping`
(`translate.py:622-676`) performs live HTTP through `client.resolve_model`
(`translate.py:636`) and `client.probe` (`translate.py:650`). Both resolve as
module attributes on the `lib.client` singleton, so the suite's existing idiom
covers it: attribute swap with orig/restore in `try/finally`, no
`unittest.mock` (worked example: `patched_chat`, `test_consensus.py:161-174`).
The case writes a real `config.json` into a temp project and builds args with
`translate._build_parser().parse_args(["ping", str(proj)])`
(`_build_parser` + `parse_args` precedent: `test_git.py:294`).

- no `consensus_*` authored -> no `consensus(...)` line is printed (byte-identical
  output to today)
- `consensus_translator` pointed at a second endpoint -> exactly one extra probe
  line, labelled `consensus(translator)`

---

## 9. Explicitly not doing, with reasons

| Item | Reason |
|---|---|
| **Materializing `consensus_<job>` keys into every config** | A fresh project would carry 6 dead blocks duplicating the global's `base_url`/`auth`. Change the global later, forget the copies, and the copies silently win for the jobs that have them. The optional key costs one line of authoring instead. |
| **Adding them to `PROVIDER_JOBS`** | Breaks `test_provider_arrays.py:83,110` and makes `init` write them (`translate.py:309-320`). Not worth a registry that means "jobs that fan out or are called directly". |
| **Packing chapters against the arbitrator's cap** | `pipeline.py:1367-1371` computes `provider_max` from the **translator's own blocks** only; the arbitrator is not in that path, today or after this change. So a small-context `consensus_translator` truncates the *merge*, not the chapter. Pre-existing; documented in Step 4's doc pass, not fixed here. |
| **`max_tokens_limit` inheritance wrinkle** | `config.local.EXAMPLES.md:269-271` already warns that an inherited `consensus` picks up `translator[0]`'s limit. With layering, a partial `consensus_translator` inherits the global's limit too. The doc line needs widening; the behaviour is correct (inherit, don't lose). |
| **A `consensus_job` trace field** | The consensus call's `model` is already logged and already rendered in the ledger column header. A second field is redundant except when `model` is null (endpoint default), which is rare and already ambiguous today. |
| **Console line naming the arbitrator** | Asserted verbatim at `tests/test_consensus.py:86`; changing it buys little, since the announce already names the job. |

---

## 10. AGENTS.md compliance

- **Migration:** `migrations/v015.py` required by §7 precedent. Must define
  `VERSION = 15`, a one-line `DESCRIPTION`, and
  `migrate(project_dir, templates_src, dry_run=False, force=False, confirm=None) -> list[str]`.
  `chain()` discovers it; no registry edit. It should delegate to
  `common.sync_templates` exactly as `v013` does.
- **Docs to update in the same change** (grep already run; these are the lines,
  not a to-do):
  - `references/file-formats.md`: the `providers` schema example (~line 223),
    the multi-model consensus bullet (~263-290), the `max_tokens_limit`
    inheritance wrinkle (~309-312), the four contractual error messages
    (~332-336), the `sync-config` rejection list (~386-387), the § Migrations
    paragraph for `v015`, and the arbitrator's absence from the packing budget.
  - `SKILL.md:358-369` (the multi-model consensus note) and `SKILL.md:511-524`
    (the per-job provider contract, incl. the `consensus` inheritance rule).
  - `README.md:700-711` (`## Tuning (config.json)`, the `providers` contract).
  - `config.local.EXAMPLES.md`: the routing table row for `consensus` (326) and
    the "synthesizes every fan-out" prose (329-341) -- this is the text the
    maintainer will re-read after the change -- plus the inheritance wrinkle at
    269-271.
  - `config.local.example.mixed.json:85` is the natural place to demonstrate a
    per-job arbitrator. Safe to add: `test_sync_config.py:724-727` computes
    `missing = PROVIDER_JOBS - named`, so extra keys in an example overlay are
    never reported as missing.
  - **Must NOT change:** `README.md:856-859` documents the log meta
    `{"job": "consensus", "consensus_for": <job>}`. That format is unchanged.
- **Tests:** `uv run tests/run_all.py` green, checks >= 2280.

---

## 11. Acceptance criteria

1. `uv run tests/run_all.py` -> 49 passed, 0 failed, **>= 2280 checks**.
2. The old resolution is gone from the runtime. Note `consensus_provider`
   itself calls `provider(cfg, "consensus")`, so the check is scoped per file:
   ```
   rg -n 'provider\(cfg, "consensus"\)' novel-translator/scripts/lib/consensus.py   # nothing
   rg -c 'provider\(cfg, "consensus"\)' novel-translator/scripts/lib/config.py      # present (the new resolver)
   ```
3. The job registry did not move -- asserted in
   `tests/test_provider_arrays.py`, not just eyeballed:
   `PROVIDER_JOBS == ("translator", "glossary", "reviewer", "annotator",
   "recap", "profile", "consensus")` and `len(PROVIDER_DEFAULTS) == 7`.
   `git diff` shows no change to `PROVIDER_DEFAULTS`.
4. **Zero behaviour change without the new key.** A project with no
   `consensus_*` key resolves to the same arbitrator block for every job:
   covered by the existing `test_consensus.py` case (b2)
   (`c["block"] is cblock`, an identity check), by the three
   `test_token_cap_ceiling.py` cases (439, 474, 485) that hand-build
   `cfg["providers"]["consensus"]` and drive `consensus.chat` end to end, and by
   the on-disk fixture `tests/review-e2e/config.json` (a real `config.json`
   with a `consensus` block and no `consensus_*` keys). Plus the new ping case
   asserting no extra probe line.
5. `git diff --stat` shows no change under `novel-translator/assets/` (no
   template change -- the synthesis prompt is job-agnostic) and none under
   `lib/logdashboard.py` or `lib/logreport.py` (log format unchanged).

---

## 12. Open questions for the maintainer

1. **Naming.** `consensus_<job>` (`providers.consensus_translator`) is the
   proposal. Alternatives: `merge_<job>`, `<job>_consensus`,
   `consensus_for_<job>`. The first sorts next to `consensus` and is what the
   resolution code reads most naturally.
2. **Unknown-suffix behaviour.** This plan **raises** on
   `consensus_translatior`. The alternative is to pass it through silently like
   every other unknown key. Raising is one line and the typo is one character;
   but it is the one place this change is stricter than the status quo, so say
   if you would rather it stay quiet.
3. **The `ping` addition (Step 3).** It is the only part of this plan that
   touches a command surface outside consensus itself. It exists because
   without it, a per-job arbitrator is the one provider in the file that
   `ping` cannot check.