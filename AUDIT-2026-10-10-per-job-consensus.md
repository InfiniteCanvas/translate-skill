# AUDIT: PLAN-2026-10-10-per-job-consensus.md

**Auditor:** parent agent (own code check) + an independent `verifier` subagent.
**Target:** `PLAN-2026-10-10-per-job-consensus.md`
**Baseline at audit time:** `uv run tests/run_all.py` -> 49 passed, 0 failed,
49 scripts, **2280 checks**.

Findings are graded against the plan's **body**, not its framing. Round 1 is
the parent's own code check; round 2 folds in the subagent read.

---

## ROUND 1 -- parent's own code check

**Verdict: no blocker. 2 major, 2 minor.** The core design (a `consensus_<job>`
sibling key layered onto the resolved global) is sound and the
backward-compatibility claim survives direct verification. The two majors are
both in the *plan's completeness*, not in its design.

### MAJOR 1 -- Step 3's test is unimplementable as written: `cmd_ping` has no test harness anywhere in the suite

The plan (§8, last bullet) defers the question: "in `tests/test_provider_arrays.py`
or a new case in the ping tests if one exists -- the sweep in Step 0 must
confirm where `cmd_ping` is covered".

**The sweep's answer is: nowhere.**

```
rg 'cmd_ping|_ping_err' tests/     -> 0 hits
```

The only occurrences of the word are prose: `tests/mock_server.py:197`
("letting tests exercise the ping chat-probe fallback"),
`tests/test_sync_config.py:721-723` ("fails at the first ping"), and
`tests/test_fatal_provider_errors.py:310` (a docstring that *motivates*
`client.probe` -- case (h) then tests `client.probe` directly, not `cmd_ping`).

`cmd_ping` (`translate.py:622-676`) performs **live HTTP**: `client.resolve_model`
at `translate.py:636` and `client.probe` at `translate.py:650`. There is no
existing harness to slot a case into, so Step 3 costs a new test surface that
the plan does not price.

**Correction -- pin the harness rather than deferring the sweep.** Both network
calls resolve as module attributes on the `lib.client` singleton, so the
idiom this suite already uses applies directly: attribute swap with
orig/restore in `try/finally`, no `unittest.mock` (`test_consensus.py:30-34`
documents it; `patched_chat` at `test_consensus.py:161-174` is the worked
example). The case is then: write a real `config.json` into a temp project,
build args with `translate._build_parser().parse_args(["ping", str(proj)])`
(the `_build_parser` + `parse_args` pattern is used at `test_git.py:294`), and
swap `client.resolve_model` / `client.probe` for recorders.

The plan must state this as new work rather than an open question.

### MAJOR 2 -- "validate the shape" undersells what the `_normalize_providers` pass must do: normalize-to-LIST is what puts the arbitrator in the run ledger

The plan (§4.2) says the new pass validates the shape of an authored
`consensus_<job>` key and does not pre-fill it. The "does not pre-fill" half is
correct and well-reasoned (§3.2). The other half is stated as a validation, and
an implementer who reads it that way gets a silent observability regression on
precisely the thing this feature is about.

`translate.py:81-83` builds the `run_start` event's `provider_jobs` map:

```python
"provider_jobs": {job: [b.get("model") or "" for b in blocks]
                  for job, blocks in sorted(cfg.get("providers", {}).items())
                  if isinstance(blocks, list)},
```

The `isinstance(blocks, list)` filter drops a bare dict. So an authored
`"consensus_translator": { ... }` left un-normalized as a dict **vanishes from
the run ledger** -- the run no longer records which model arbitrated the
translator, which is the one thing an operator reads after a fan-out goes
wrong.

**Correction:** state the rule as **"normalize the shape to a one-element list;
do NOT pre-fill keys"**, and name `translate.py:81-83` as the reason the shape
pass is load-bearing rather than cosmetic. As a bonus this makes the plan's
rejected alternative (`consensus_job` trace field, §9) clearly the right call:
the run ledger already carries the arbitrator's model for free, at run level,
with no log-schema change at all.

### MINOR 3 -- `consensus_consensus` is an accepted-but-inert suffix

`CONSENSUS_PREFIX + "consensus"` = `consensus_consensus`, and `consensus` **is**
a member of `PROVIDER_JOBS` (`config.py:6`). So `providers.consensus_consensus`
passes the plan's §4.2 suffix validation, normalizes cleanly, and is then never
read by anything.

Verified dead: `consensus` can never hold more than one block (rejected at
`config.py:229-231`), and `consensus.chat` is never called with `job="consensus"`
-- the only `config.provider(cfg, "consensus")` in the tree is `consensus.py:231`,
inside the `len(blocks) > 1` branch, and every job name that reaches
`consensus.chat` is one of `translator`, `glossary`, `reviewer`, `annotator`,
`recap`, `profile`.

Not a bug -- just an undocumented dead configuration the next reader trips over.
Either exclude `consensus` from the valid suffix list (one line) or state in the
plan and the docstring that it is accepted-and-inert.

### MINOR 4 -- three doc mirrors are left "to be confirmed by grep"; the grep is done

AGENTS.md names six mirrors. The plan (§10) confirms two and says of the rest:
"only if they restate the consensus job by name. To be confirmed by grep during
implementation." That grep has now been run:

| File | Lines | What it states |
|---|---|---|
| `novel-translator/SKILL.md` | 358-369 | the multi-model consensus note: "ONE `consensus`-provider call merges the labeled candidates" |
| `novel-translator/SKILL.md` | 511-524 | the per-job provider list, incl. "`consensus` never inherits the array -- exactly one block, defaulting to the translator's first block" |
| `novel-translator/README.md` | 700-711 | `## Tuning (config.json)` -- the `providers` contract, incl. the `consensus` inheritance rule |
| `novel-translator/README.md` | 856-859 | the log meta `{"job": "consensus", "consensus_for": <job>}` -- **correct as-is, must NOT change** |

`SKILL.md` and `README.md` both state the contract in prose and both go stale.
The plan should name them rather than defer.

Also worth naming: `novel-translator/config.local.example.mixed.json:85` carries
the `consensus` block as a **bare dict** (the legacy shape). It is the natural
place to demonstrate a per-job arbitrator, and it is safe to do so --
`test_sync_config.py:724-727` computes `missing = PROVIDER_JOBS - named`, so
**extra** keys in an example overlay are never reported as missing.

---

## Claims verified correct as written -- do not change these

- **The old resolution has exactly one call site.** Tree-wide grep for
  `provider(cfg, "consensus")` returns `lib/consensus.py:231` and nothing else.
  So Step 2 really is a one-line change plus its docstring.
- **All six fan-out jobs reach `consensus.chat`**, so the key naming is exactly
  right: `pipeline.py:620, 698, 1444, 1650, 1703, 1756`,
  `profile.py:78`, `review.py:216`, `review_notes.py:260`, `story.py:173`.
- **Packing does not consult the arbitrator.** `pipeline.py:1355, 1367-1371`
  computes `provider_max = min(config.block_cap(b) for b in translator_blocks)`
  over the translator's own blocks only. The plan's §9 claim ("a small-context
  `consensus_translator` truncates the *merge*, not the chapter") is correct.
- **`merge_overlay` needs no change**, and the plan's §4.4 reasoning holds on
  both paths (`config.py:329-340`): absent in project -> `_deep_merge` adds it
  wholesale; present -> the single-dict form merges into every block. Either
  way `apply_overlay`'s validation call (`config.py:384`) runs
  `_normalize_providers`, so a two-block `consensus_<job>` in an overlay is
  rejected before anything is written.
- **Leaving `PROVIDER_JOBS` alone is load-bearing**, exactly as the plan says:
  `test_provider_arrays.py:83` asserts `PROVIDER_JOBS[-1] == "consensus"` and
  `:110` asserts `sorted(norm) == sorted(config.PROVIDER_JOBS)`. Registering
  the new keys would break both.
- **The backward-compatibility claim holds under direct test.** Hand-built cfgs
  that set only `providers.consensus` and drive `consensus.chat` end to end
  exist at `test_token_cap_ceiling.py:439, 474, 485`. With no `consensus_*`
  key present, `consensus_provider` returns `provider(cfg, "consensus")`
  unchanged, so those cases are unaffected.
- **`v013` reading raw `providers.consensus` is genuinely harmless.** Migrations
  are version-stamped and never re-run (`translate.py:329-337` stamps at init;
  `cmd_migrate` stamps per step), so a config sitting at version 13 cannot
  contain a v15-era key unless hand-written before upgrading. The plan's
  reasoning is right.
- **The nested-shape rejection (§3) is correct.** `providers.consensus` must be
  a block or an array of blocks (`config._provider_blocks`, `config.py:161-180`)
  and a block *is* a dict, so `{"translator": {...}}` would be wrapped into a
  one-element list and sent with no `base_url` -- falling back to the
  hard-coded `DEFAULT_BASE_URL` (`config.py:97`). That is precisely the
  un-pointing failure `config.py:328` was written to prevent.
- **The read-time-layering decision (§3.2) is right.** `test_consensus.py:42-43`
  states outright that cfg dicts are "hand-built (never load_config'd)", so
  resolving only at load time would make the suite's cfgs resolve differently
  from real ones.

---

## Required corrections before implementation

1. **MAJOR 1** -- replace the deferred ping-test sweep with the pinned harness
   (`client.resolve_model` / `client.probe` attribute swap +
   `translate._build_parser().parse_args(["ping", str(proj)])` + temp
   `config.json`), and mark it as new test surface.
2. **MAJOR 2** -- restate §4.2 as "normalize to a one-element list, do not
   pre-fill keys", citing `translate.py:81-83`.
3. **MINOR 3** -- decide and state the fate of `consensus_consensus`.
4. **MINOR 4** -- name `SKILL.md:358-369, 511-524` and `README.md:700-711` in
   the doc list; mark `README.md:856-859` as explicitly unchanged.

**All four applied to the plan.**

---

## ROUND 2 -- re-check of the REVISED plan

Round 1 attacked the plan as first written. Round 2 attacked the plan *after*
those four fixes, concentrating on the subagent's checklist and on the areas
round 1 had not covered.

**Scope note, stated plainly:** an independent `verifier` subagent was launched
in parallel and **produced no output across repeated waits**; it was cancelled
without findings. Round 2 is therefore the parent's own direct verification, not
a second opinion. The items below are the ones a subagent was asked to check,
each answered against the code rather than assumed.

**Verdict: 2 major, 1 minor, all in the plan's specification rather than its
design. No blocker. The design survives both rounds unchanged.**

### MAJOR 3 -- acceptance criterion 2 was self-defeating

The plan's original criterion 2 read:

```
rg -n 'config\.provider\(cfg, "consensus"\)' novel-translator/scripts/   # nothing
```

`consensus_provider` **itself** calls `provider(cfg, "consensus")`. So the
criterion, executed after a correct implementation, returns a hit and fails.
An acceptance test that a correct implementation fails is worse than no test.

**Correction (applied):** scope the check per file -- zero hits in
`lib/consensus.py`, a hit present in `lib/config.py`.

### MAJOR 4 -- §4.1 named two different functions for the same read

The revised §4.1 said "use `provider_list` (dict-tolerant) for the optional
key" and, two lines later, layered `_provider_blocks(cfg["providers"], key)[0]`.
These are not interchangeable:

| | `_provider_blocks` (`config.py:161-180`) | `provider_list` (`config.py:449-459`) |
|---|---|---|
| bare dict | wraps to `[dict]` | wraps to `[dict]` |
| empty array | raises `must not be an empty array` | returns `[]` -> `[0]` raises `IndexError` |
| non-object element | raises `providers.X[i] must be an object` | passes it through |
| wrong top-level type | raises with the key name | passes it through |

`_provider_blocks` is correct: its three messages interpolate the key, so a
malformed `consensus_translator` says
`providers.consensus_translator must be a provider block (object) or an array
of blocks`. It also keeps `consensus_provider` correct on hand-built cfgs, which
never passed through `load_config` and so were never validated by §4.2.

**Correction (applied):** §4.1 now specifies `_provider_blocks` only, with the
comparison table's reasoning, plus the identity requirement (return
`provider(cfg, "consensus")` **unchanged**, not a copy -- case (b2) asserts
`c["block"] is cblock`).

### MINOR 5 -- the ping dedupe reuses a variable that is reset every iteration

§6's dedupe is specified against "a block not already probed". The main loop's
`seen` dict (`translate.py:628`) is **re-created on every job iteration**, so
an implementer who reaches for the obvious in-scope name gets an empty set at
the end of each job and probes every arbitrator. Also unspecified: the failure
path (a dead arbitrator must set `failed = True` and exit 2 like any other
unreachable provider).

**Correction (applied):** §6 now pins a separate global probed-set, names the
comparison fields, and states the failure behaviour.

---

## Evidence added in round 2 -- claims upgraded from argument to execution

Round 1 argued reachability from reading `config.py`. Round 2 ran the code.

**The typo guard is reachable and currently a silent no-op.** Feeding a
typo'd key through `_normalize_providers` today:

```
providers.translator    + "consensus_translatior"
  -> normalized keys: [annotator, consensus, consensus_translatior,
                       glossary, profile, recap, reviewer, translator]
  -> typo key survived untouched, reachable silently: True
```

So `consensus_translatior` loads cleanly, is read by nothing, and the
translator quietly keeps merging through the global arbitrator. This is the
`075269f` class ("warn instead of silently ignoring"), reachable by one
character on the exact key this change introduces. **The guard is not
speculative** -- it defends a state the current code demonstrably permits.

**A bare-dict per-job key would vanish from the run ledger.** Same harness,
`"consensus_translator": {"model": "arb"}` (the legacy single-block shape the
codebase still supports everywhere):

```
value: {'model': 'arb'} | type: dict
run_start provider_jobs filter isinstance(list) keeps it: False
```

This is MAJOR 2 measured, not read: had the implementer "validated" the shape
without normalizing it, the run would stop recording which model arbitrated
the translator.

---

## Items checked this round that the plan got RIGHT

- **`chain()` needs no registry edit.** Verified in
  `novel-translator/scripts/migrations/__init__.py:36-65`: it globs `v*.py`,
  sorts by number, and validates `VERSION` against the filename plus the
  presence of a callable `migrate`. `current_version()` resolves through the
  same lookup, so `init` picks up v015 automatically. The plan's migration
  contract is correct as written.
- **Every job name reaching `consensus.chat` is a real job.**
  `translator` (`pipeline.py:1444`), `glossary` (620, 698, 1703; `review.py:216`),
  `reviewer` (1650; `review_notes.py:260`), `annotator` (1756),
  `recap` (`story.py:173`), `profile` (`profile.py:78`). All six are in
  `PROVIDER_JOBS`, so every one is a legal `consensus_<job>` suffix.
- **Non-translator jobs still cannot exceed the arbitrator's declared
  `max_tokens`.** They pass `max_tokens=None` (e.g. `pipeline.py:1756`), so
  `c_max` collapses to the arbitrator's own value (`consensus.py:257`) and
  `v013`'s "`_task_caps` only reports the translator" reasoning holds unchanged
  under per-job arbitrators.
- **A `consensus_<job>` key in an example overlay breaks no test.**
  `test_sync_config.py:724-727` computes `missing = PROVIDER_JOBS - named`;
  extra keys are never "missing". `config.local.example.mixed.json:85` is safe
  to extend.
- **`tests/review-e2e/config.json:54-57`** is an on-disk `config.json` with a
  `consensus` block and no `consensus_*` key -- a free end-to-end back-compat
  check, now named in acceptance criterion 4.

---

## Round 2 verdict

**Clean enough to implement.** The design was attacked twice and did not move:
a sibling `consensus_<job>` key layered onto the resolved global arbitrator is
correct, backward compatible, and small. Every finding was a specification gap
-- a self-defeating acceptance criterion, two functions named for one read, a
reset-each-iteration variable, an uncosted test harness -- and all are now closed
in the plan text.

The one thing this audit could not supply is an independent second opinion: the
`verifier` subagent returned nothing and was cancelled. Every claim above
carries a `file:line` or a command's output, so the maintainer can re-derive
each one directly.

---

## ROUND 3 -- findings raised DURING implementation

Rounds 1-2 audited the plan. This round records what actually surfaced while
building it -- one pre-existing defect, one validator that turned out to encode
a now-incomplete assumption, and three of my own test-authoring errors, each of
which a check caught rather than a reviewer.

### MAJOR 5 -- the shipped `config.local.example.mixed.json` carried a DUPLICATE JSON key (pre-existing)

Adding the per-job demonstration to the mixed example turned up:

```json
"max_tokens": 128000,
"max_tokens_limit": 131072,
"max_tokens_limit": 131072
```

`max_tokens_limit` appears twice. Confirmed pre-existing and not mine
(`git diff` on the file was empty before this change). Python's `json.loads`
takes the last one silently, so it is functionally harmless -- but this file is
the example operators copy, and it teaches a shape that a strict JSON reader
would reject as a duplicate key.

Fixed in the same edit. **It is a defect in the repo, not in this plan**, and it
would still be there had this change never happened.

### MAJOR 6 -- the example validator encoded an assumption v015 invalidates

`tests/test_sync_config.py` case 10 asserts two invariants over every block in
each shipped example: "uses api_key_env, not api_key" and "every block carries
the 64k output budget". Adding the deliberately partial
`"consensus_translator": {"model": "glm-5.3"}` made **both** fail.

Neither failure was a real problem with the example. The validator read the
AUTHORED text, so it could not see that a partial per-job block inherits
`api_key_env` and `max_tokens: 128000` from the global `consensus` block at
load time. It was flagging the exact shape the examples are meant to teach.

Fixed by resolving before validating: `_effective_blocks()` yields
`(job, block)` for every provider block **as it will run**, layering a
`consensus_<job>` block onto the global one the same way
`config.consensus_provider` does. The checks now model runtime rather than
authored text.

The change does not weaken them -- an authored `max_tokens: 1000` on a per-job
key still merges to `max_tokens: 1000` and is still caught. It also fixed two
other latent gaps for free: the MiniMax-M3 / Flash-Preview reasoning checks now
see through per-job inheritance too, and `small` is labelled by model rather
than by array index, so a failure names the block that failed.

### MINOR -- three test-authoring errors, each caught by running rather than reading

Recorded because they are the checks working, not because they are interesting:

1. `test_consensus.py` j1 asserted "no merge used the global block" while the
   annotator's merge legitimately does. The implementation was right; the test
   conflated the two calls. Fixed by clearing `fake.calls` between them so each
   merge is classified on its own -- which also made the test prove what it
   claimed: both jobs carry the SAME candidate models, so only arbitrator
   selection can explain the difference.
2. `test_ping.py` e2 asserted a `ping: one or more providers unreachable`
   summary that `_fail` writes to **stderr**. The harness now merges both
   streams. The behaviour under test (exit 2) was correct from the start.
3. `test_migrate.py` 17e expected "5 fan-out job(s)" where all six fan out
   (translator plus five inheriting the array). The migration was right.

In all three the implementation was correct and the assertion was wrong. Worth
recording because the tempting move in each case was to relax the implementation
to match the test.

---

## Final state

`uv run tests/run_all.py` -> **50 passed, 0 failed (50 scripts, 2339 checks)**,
from a baseline of 49 scripts / 2280 checks.

Acceptance criteria from the plan, each verified directly:

| # | Criterion | Result |
|---|---|---|
| 1 | Suite green, checks >= 2280 | 2339 |
| 2 | Old resolution gone from the runtime | `provider(cfg, "consensus")` survives ONLY inside `consensus_provider` (`config.py:564,566`); zero hits in `consensus.py`, which now calls `config.consensus_provider(cfg, job)` at `consensus.py:247` |
| 3 | Registry unmoved | `git diff` on config.py shows only READS of `PROVIDER_DEFAULTS["consensus"]`; neither `PROVIDER_JOBS` nor `PROVIDER_DEFAULTS` is redefined |
| 4 | Zero behaviour change without the new key | `test_consensus.py` b2 (dict identity), `test_token_cap_ceiling.py` x3, `test_ping.py` a1-a3 (byte-identical output), and the on-disk `tests/review-e2e/config.json` fixture |
| 5 | No template, no log-format change | `git diff --stat` touches nothing under `assets/`, `lib/logdashboard.py`, or `lib/logreport.py` |