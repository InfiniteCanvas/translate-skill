# Audit — `PLAN-2026-10-07-logging.md`

**Date:** 2026-10-07
**Auditor:** Mavis (author of the plan; adversarial self-audit plus an
independent verifier pass)
**Subject:** `PLAN-2026-10-07-logging.md` @ working tree, HEAD `3bbe4af`
**Verdict:** **Do not implement as written.** The design is sound; three
defects would ship broken behavior and five more would ship something the
docs contradict.

The goal (split the log scope in two: orchestration vs. a full per-chapter
model trace) is right, and the core mechanism — the `run_id` join key, the
per-event routing table, capture-at-hook-construction for the fan-out — are
the right ideas. The defects are concentrated in three places: a promised
property the design cannot deliver, a misattribution the plan never noticed,
and a migration that would silently hide a config change.

---

## Critical

### C1 — The "timeline stays complete when `log_llm: false`" property is unreachable

**Where:** plan §3.5; `novel-translator/scripts/lib/consensus.py:60-70`

The plan sells this: *"with `log_llm: false` there are no tier-2 files at
all, but tier 1 still emits `llm_call`, so the timeline stays complete at any
verbosity."*

The single logging owner for every model call is:

```python
def _trace_hook(project_dir, cfg, job, extra=None):
    enabled = bool(cfg.get("log_llm", config.DEFAULTS["log_llm"]))
    def hook(meta: dict) -> None:
        if enabled:
            logger.log_event(project_dir, {"job": job, **(extra or {}), **meta})
    return hook
```

`enabled` wraps the **entire** hook body. With `log_llm: false` the hook is
inert, so the proposed tier-1 `llm_call` line never runs either. The plan
never mentions restructuring this function, and §5 phase 2 lists only
"ContextVar capture + `llm_call`" — which cannot work as written.

**Fix:** the hook must emit twice, under two independent gates:

```python
def _trace_hook(project_dir, cfg, job, extra=None):
    io_on  = bool(cfg.get("log_llm", ...)) and bool(cfg.get("log_prompt_bodies", True)) or ...
    orch_on = bool(cfg.get("log_orchestration", ...))
    def hook(meta):
        if orch_on and meta.get("event") == "llm_response":
            logger.log_event(project_dir, {**tier1_summary(meta)})   # metadata only
        if io_on:
            logger.log_event(project_dir, {"job": job, **(extra or {}), **meta})
```

Pin both gates in §4.1 with a check that asserts a tier-1 `llm_call` exists
with `log_llm: false`, so this contradiction cannot reappear.

### C2 — Recap backfill files a predecessor chapter's model call under the wrong chapter

**Where:** plan §3.3; `novel-translator/scripts/lib/story.py:208-234`

The ContextVar is set to the chapter being translated. But
`story.ensure_recap`, called at the top of every attempt
(`pipeline.py:1019`), generates the recap for the **predecessor**:

> *"else a one-call backfill of the predecessor, which also saves the state"*
> — and it reads `project.read_chapter(prev_path)` from `translated/`, then
> prompts `recap.md` with **the predecessor's** title and body.

So translating Chapter 8 when Chapter 7's recap is missing emits a full
`llm_request`/`llm_response` pair whose entire content is Chapter 7, filed
into `logs/chapters/Chapter_0008/`. Chapter 8's "full translation pipeline
run" would contain a foreign chapter's model exchange, with no label
distinguishing them.

**Fix:** decide and state it. Recommended: tag the event with the target
(`chapter: <current>`, `recap_target: <predecessor>`) so it stays in the
invocation's chapter file but is honestly labeled; the alternative — routing
it to the predecessor's directory — splits one attempt's calls across two
files and makes the chapter report lie. Whichever is chosen, §4.3 needs a
check pinning it, because the bug is invisible until someone reads a
backfill-heavy run.

### C3 — `v010` as specified hides the retention change it is supposed to ship

**Where:** plan §3.5, §5 phase 1; `migrations/common.py:27-46`; `migrations/v009.py:48-50`

The plan says `v010` delegates to `standard_step()`, which is
`materialize_config` + `sync_templates`. But `materialize_config` writes the
**entire merged form**:

```python
merged = config.load_config(project_dir)      # DEFAULTS deep-merged underneath
...
config.save_config(project_dir, merged)        # writes EVERY default explicitly
```

The repo already knows this is a trap. `v009`'s docstring:

> *"Rewrite the raw file, not load_config's merged form: load_config would
> deep-merge the NEW defaults underneath and report the change as 'provider
> blocks normalized', **hiding which numbers actually moved**."*

Raising `log_llm_keep_runs` from 5 to 10 is exactly the `v009` case: a
default bump that must be visible. Shipped as a bare `standard_step()`, the
user sees `[ok] config: materialized 3 new key(s)` and never learns retention
changed from 5 to 10.

**Fix:** hand-write `v010` in the `v009` shape — rewrite the raw file when
`log_llm_keep_runs` exactly equals `5` (the established "untouched default"
rule, which deliberately cannot distinguish "wanted 5" from "never set it"),
then materialize the new keys, then `sync_templates`. State the ambiguity and
its recoverability, the way `v009` does.

---

## High

### H1 — The routing table contradicts itself on `tn_recheck`

**Where:** plan §3.2; `scripts/translate.py:1010`

The table routes `tn_recheck` to tier 2. Its only emitter:

```python
logger.log_event(project_dir, {"event": "tn_recheck", **result})
```

One aggregate event for the whole command, spanning N chapters, with **no
`chapter` field**. Under §3.2's own fallback rule ("a tier-2 event with no
resolvable chapter falls back to `logs/project/`"), it lands in the project
bucket — exactly where the table says it should not be.

**Fix:** route `tn_recheck` to tier 1 (it summarizes a command's outcome), or
split the emitter into per-chapter events. The plan must pick one; §4.2's
"each of the 15 named events reaches its specified tier" check cannot pass
as written.

### H2 — `glossary_cleanup` routing breaks a documented reader path and the plan's own principle

**Where:** plan §3.2, §8; `README.md:236-243`

`README.md:240-241` currently tells the reader:

> *"The `glossary_cleanup` trace events in `logs/llm-*.jsonl` show each
> removal (source + reason) and the kept terms"*

The plan moves that event to a **different directory** under a **different
filename**. A reader following the README lands in the wrong place twice over.

Worse, it contradicts the plan's own tier definition. §3.2 says tier 2 is
"model IO and the pipeline's reading of it"; `glossary_cleanup` reports
removed/kept **terms** — a summary of what the pipeline did, i.e. tier 1. Only
the judgment LLM call it made belongs in tier 2.

**Fix:** `glossary_cleanup` → tier 1. This also restores the README's claim to
something merely re-pathed rather than false, and §4.7's stale-glob check
(`README.md:756-781`) should be extended to cover `README.md:236-243`.

### H3 — Retention pruning can destroy `index.jsonl`

**Where:** plan §3.1, §4.1 ("tier-2 prune keeps the newest `log_chapter_keep_runs`
files per chapter dir")

`index.jsonl` lives in the chapter directory and is a `.jsonl`. It is
append-only and never rewritten, so it is always among the **oldest** files by
mtime — precisely what a "keep the newest N" prune deletes first. The plan
never excludes it.

**Fix:** exclude by name (`index.jsonl`), or give it a distinct prefix that
the run-file glob cannot match. Add a §4.1 check: after pruning a chapter dir
down to N files, `index.jsonl` still exists.

### H4 — The reset contract cannot be satisfied by module aliasing, and has three consumers

**Where:** plan §6 ("keep `_run_path` as the documented alias and clear both")

A plain assignment `logger._run_path = None` cannot mutate a separate cache
dict. Worse, the plan names only one consumer; there are three:

- `tests/test_cleanup_flow.py:215` and `:318`
- `tests/test_consensus.py:135` (`reset_globals`)
- `tests/test_logger.py:68` (the documented contract itself)

**Fix:** `log_event` must treat `_run_path is None` as an explicit reset
signal and clear the cache dict when it observes it. The plan should state
this mechanism, not just the intent, because the naive version silently
breaks all three suites.

### H5 — ContextVar lifecycle unspecified; a raising chapter leaks its scope

**Where:** plan §3.3

`run_range` catches per-chapter exceptions and continues
(`pipeline.py:1571`). If `run_chapter` sets the chapter scope and raises —
malformed frontmatter, an `LLMError` escaping a stage — without clearing it,
the stale chapter stays active for everything logged between that failure and
the next chapter's entry. `KeyboardInterrupt` propagates and aborts, so that
path is harmless.

**Fix:** mandate a `try/finally` around the entire `run_chapter` body that
clears the scope, and add a §4.3 check that a chapter raising mid-run leaves
the next chapter's files uncontaminated.

---

## Medium

### M1 — Stage emission sites are unspecified, and the natural hook covers only 7 of 8

`advance()` (`pipeline.py:1022`) is the obvious instrumentation point — it
already persists `state["stage"]` — and it fires at seven sites: VALIDATE
(`:1277`), BALANCE (`:1296`), FAITH (`:1347`), GLOSSARY_EXPAND (`:1377`),
TN_GENERATE (`:1427`), TN_DEDUP (`:1450`), ASSEMBLE (`:1475`). **TRANSLATE
is entered at `:1070` with no `advance` call**, so it needs its own emission.

The plan specifies payloads but never emission sites, which is the difference
between a spec and an outline. Related: `elapsed_s` requires wrapping each
stage block, and those blocks are guarded by
`if failed_stage is None and start_idx <= _STAGE_IDX[...]` — the delicate
crash-resume logic. Also, a **resumed** chapter starting at BALANCE emits no
TRANSLATE event, so §4.3's "8 stages on a clean chapter" holds only for a
fresh chapter; the plan should say events fire for stages actually entered.

### M2 — `materialize_config` bakes every default in permanently

Related to C3 but a distinct consequence worth stating: because
`save_config(merged)` writes the full merged form, after `v010` every existing
project carries `"log_llm_keep_runs": 10` as an **explicit literal**. That
value will thereafter no longer track `DEFAULTS` — a future default bump
reaches only new projects. `v009` accepts exactly this trade-off and documents
it; the plan does not.

### M3 — Where prompt-body stripping happens is unspecified

The plan says `log_prompt_bodies: false` drops `prompt`/`response`. Doing it
in the logger means reading `config.json` per event. Today `_keep_count`
(`logger.py:72`) reads config once per **run-file creation**, not per event —
50 reads per chapter is a real regression. Resolve and cache the flags once
per run.

### M4 — Chapter-stem sanitization has no collision rule

The plan mentions sanitizing a hostile stem but not what happens when two
distinct stems sanitize to the same directory name. Silent merging of two
chapters' logs is the failure mode. Needs a collision-resistant suffix (short
hash of the original stem) or an explicit refusal.

### M5 — `index.jsonl` is never pruned and references dead runs

After per-chapter run files are pruned away, the index keeps `open`/`close`
lines naming run ids whose files no longer exist. Either prune the index
alongside its runs or mark entries stale.

---

## Low

### L1 — Acceptance criterion 3 is grep-based and will false-positive

§7 says `grep -c '"prompt"' logs/run-*.jsonl` returns 0. But `glossary_review`
embeds a `findings` list of arbitrary reviewer text and `review_fix` embeds
`applied`/`skipped` — either can legitimately contain the literal substring
`"prompt"`. §4.2 states the correct version (parse JSONL, assert no line *has*
a `prompt` key); §7 contradicts it. Use the parse-based check in both.

### L2 — `--last N` is in the interface, undefined, and untested

It appears in the §3.6 usage string and has no semantics and no check.

### L3 — `log_orchestration: false` leaves `index.jsonl` undecided

If tier 1 is off, is the per-chapter index still written? It is chapter
metadata, but the plan never says.

### L4 — `--run RUN_ID` is unusable in practice

`RUN_ID` embeds `<pid>`, which the user cannot know. Needs a listing step (the
index provides one) or prefix matching. Untested and unspecified.

### L5 — The ContextVar is a house-style deviation and should say so

The codebase uses plain module globals for exactly this process-scoped state:
`pipeline._TOKEN_CAP_WARNED` (`pipeline.py:754`), `consensus._ANNOUNCED`
(`consensus.py:30`). The ContextVar is strictly more correct here — worker
threads read it safely, and the capture-at-construction trick is the only
thing that makes the fan-out work — but introducing a third pattern deserves
a sentence of justification in the plan.

---

## Claims verified as correct

The audit checked these and found no error:

- Run-file naming, project re-pointing, retention-by-mtime, and
  `_LOG_LOCK` serialization, all at `logger.py:91/99/103/81-88/45`.
- `log_llm` gates only the model lines, at `consensus.py:64`.
- The full event inventory and every `file:line` reference in §2.
- `client.py` needs no change: `_request_meta`/`_response_meta`
  (`client.py:203-232`) already carry everything the plan requires.
- Twelve `consensus.chat` call sites, at the six `pipeline.py` lines listed
  plus the five module-level ones.
- `test_consensus.py:124` globs `logs/llm-*.jsonl`, so §4.6's required change
  to that helper is accurate.
- `pipeline.parse_range` (`pipeline.py:268`) is the right spec resolver to
  reuse.
- The ContextVar capture-at-hook-construction reasoning is correct, including
  the `ThreadPoolExecutor`-does-not-copy-contextvars detail and the fact that
  `run_range` is strictly sequential.
- The exit-code table matches the house convention (`CliError` → 2,
  `pipeline.py:268` region / `translate.py:1881-1883`; 1 for "found nothing",
  per the `glossary search` / `tn` precedent).
- `logs/` is gitignored and in the `vcs.py` core rules (`vcs.py:32`), so no
  gitignore work is needed.

## Claims I could not verify

None material. The one place the plan asserts behavior without a code
citation — where the `result` / `chunk` / `feedback` events are emitted — is
under-specification (M1), not a false claim.

## Plan strengths worth preserving

1. **The two-tier split itself**, and the rule that each event belongs to
   exactly one tier with no duplication. That is what makes "tier 1 contains
   zero prompt lines" a checkable invariant.
2. **`run_id` as a single join key** across both filenames, `index.jsonl`, and
   every line's fields — cheap, and it makes cross-tier analysis trivial.
3. **The explicit per-event routing table** with a documented default. It is
   unusual to specify routing as data rather than as scattered conditionals,
   and it is directly testable.
4. **Capture-at-hook-construction for the consensus fan-out**, correctly
   reasoned through the contextvar-propagation gap.
5. **Retention split by tier** rather than one global knob, and
   **`report.md` rendering all retained runs** for a chapter.

## Recommended disposition

Fix C1, C2, C3, H1-H5 before implementation — all seven are small, localized
edits to the plan text. M1-M5 and L1-L5 are worth folding in while the plan is
open; none blocks starting. Then implement in the §5 phase order, which is
correct as written.

Nothing in this audit questions the goal or the two-tier architecture. The
design is worth building; the plan as written is not yet a spec you can hand
to an implementer without answering these questions first.

---

## Addendum — an independent audit ran in parallel (2026-10-07)

A second, independent read of v1 was run while this audit was being written. It
confirmed every one of the 16 findings above (it independently reached C1, C3,
H1-H5, H3, M1, L1 and the recap-attribution problem), checked 30+ of the plan's
`file:line` claims and found them accurate, and added **18 findings of its own**
(F1-F18), including two that overturned a core v2 design decision:

- **F2** — `cmd_tn` never enters `run_chapter`, so an ambient chapter scope
  would have left every chapter's annotator IO in the project bucket.
- **F3** — the recap misattribution was worse than first assessed: the line is
  tagged with the scope's chapter, so this audit's own proposed check (assert
  chapter 7's file has no chapter-8 tag) **passes with the defect live**.
- **F4** — `run_id` was asserted as a join key but never defined as a stored
  value; derived per path, it drifts a second between tiers and silently breaks
  every cross-tier query.
- **F6** — `logs/project/` had no retention rule at all.
- **F17** — the plan's "unwritable `logs/`" check cannot fail on this NTFS box:
  the auditor ran it, and `os.chmod(dir, 0o500)` yields mode `0o40555` with
  writes still succeeding. It passed while exercising nothing.

**Resolution.** The plan is now **v3**. The ContextVar chapter scope was
removed entirely in favour of explicit `chapter=` threading, which deletes the
scope-leak hazard, the house-style deviation, F2, and F3 at the root rather
than patching each; the fan-out still works because the value is bound in the
`_trace_hook` closure before `pool.submit`. Both audits' findings are traced in
the plan's v3 changelog. Nothing from either audit was deferred without a
written disposition.

---

## Addendum 2 — verification of the round-3 (v4) findings (2026-10-07)

Round 3 ran three per-workstream adversarial agents and integrated **R1-R16**
into the plan as v4. Each claim was re-verified against the tree:

| Finding | Verdict | Evidence |
|---|---|---|
| **R1** suffixed manifest entries collide | **CONFIRMED — real bug in v3** | `project.py:18` `CHAPTER_RE = r"^Chapter_([0-9]{1,4})([a-z]?)\.md$"`; `Chapter` keeps `number: int` **and** `suffix: str` (`project.py:22-27`); `discover()` sorts by `(number, suffix.lower())` (`:64`). `Chapter_0042a.md`/`0042b.md` both carry `number == 42`, so v3's `Chapter_{number:04d}` merges them. `tag` at `pipeline.py:900` is likewise number-only, so v3 pointed at a non-canonical "canonical" string |
| **R2** `chapter=` value space triple-inconsistent | **CONFIRMED** | manifest filename `Chapter_0042a.md` (`project.py:25`) vs `tag` `[Chapter_0042]` (`pipeline.py:900`) vs dir `Chapter_0042` (v3 §3.1); `CHAPTER_RE` accepts 1-4 digits, so padding varies |
| **R3** crash-path `chapter_end` unimplementable; `skipped` missing | **CONFIRMED** | `run_chapter` returns `"skipped"` at `pipeline.py:903-905`; v3's enum is `translated\|needs-review\|crashed`. `run_range` special-cases skip at `:1584-1585`. Aggravating detail: `main` calls `os._exit(130)` at `translate.py:1901` on a fan-out Ctrl-C, so nothing after it runs |
| **R4** `index.jsonl` under `log_orchestration: false` undecided | **FAIR** | v3 §3.8 implied it via "tier 2 unchanged" but never stated it |
| **R5** `logs/project/index.jsonl` has no writer | **CONFIRMED** | v3's layout lists only `logs/project/run-*.jsonl`; `--list`/`--run` need the registry |
| **R6** §4.6 under-reported existing-suite breakage | **CONFIRMED — a miss in v3** | `test_logger.py:74` `events_in()` globs `llm-*.jsonl`; `:83` `run_name_ok()` regex-pins `^llm-\d{8}-\d{6}-.+-\d+\.jsonl$`; `test_cleanup_flow.py:249` globs `llm-*.jsonl` then asserts `glossary_cleanup`/`attempt`/`balance_advisory` exist. v3 caught `case_3_prune`/`case_4_two_projects` and missed both helpers |
| **R7** never-raises contract only covered `OSError` | **VALID** | `logger.py:110` catches `OSError`; added serialization can raise `TypeError` |
| **R8** two dueling gate points | **VALID — defect introduced by v3** | v3 §3.3 has the hook read `cfg`; §3.8 resolves flags in the logger cache. They disagree where no `config.json` exists (the hook's `cfg.get(key, DEFAULTS[key])` succeeds; `_keep_count` reads the file) |
| **R9** `calls`/`tokens`/`elapsed_s` have no source | **VALID** | v3 specifies the payload, never the accumulator; and the reset does not clear it |
| **R10** `tn_recheck` closure built once pre-loop | **CONFIRMED** | `tn_recheck.py:95-98` defines `default_chat`, `:100` binds `do_chat`, called at `:226` — threading is a protocol decision, as stated |
| **R11** `degraded` sites enumerated | **CONFIRMED** | consensus `:142-145` (single survivor) and `:172-176` (consensus call failed); `:136-139` is the per-candidate warn, correctly assigned to `llm_call`'s `error` |
| **R12** `run_start`/`run_end` must leave `translate.main` | **CONFIRMED** | `translate.py:1875-1904` is `args.func(args, project_dir)` in a try/except with `resolve_project_dir(args)` only — no cfg, manifest, or counts in scope |
| **R13** logs CLI underspecified | **CONFIRMED (my first read was wrong — see correction note)** | §3.9:678-680 already gets it right: `--json` suppresses the stdout `[ok]`/`[warn]` markers, and §3.9:684 routes the exit-1 `[FAIL]` line through `_fail`, which is stderr (`translate.py:64-65`). The stdout markers are real — `[init] translating N chapter(s)` at `translate.py:922`, `[ok] already translated - skipped` at `pipeline.py:904`. The verification pass initially called R13's mechanism wrong by reading the compressed changelog phrase instead of the body |
| **R14/R15** report source + doc-mirror gaps | **Specification items** | not falsifiable as claims; accepted as stated |
| **R16** stale test_migrate head references + fixture | **CONFIRMED (my first read was wrong — see correction note)** | Stale refs confirmed at `test_migrate.py:24-25` ("chain() == [v001…v008] with current_version() == 8"), `:191` ("real chain head (8 since v008)"), `:540-541` ("exactly [v001…v008], head version 8") — head is 9 today, 10 after `v010`. The "hardcoded-`5` fixture" is not a claim about existing code: §4.6 **instructs** the new v010 case to write a literal `5` so the rewrite branch is exercised, since folding `config.DEFAULTS` post-v010 yields 10. My grep found no hardcoded `5` because none exists yet — which is what the instruction calls for |

**Tally:** **all 16 confirmed or valid.** Round 3 found no defect in v3's
goal, tier architecture, or retention model — its findings cluster in
chapter-identity naming (R1, R2), the crash path (R3, R5), test-suite
breakage (R6), and implementation-detail gaps that stop short of a spec
(R7-R12, R14-R16).

### Correction to this addendum

This verification pass initially recorded R13 as "right in substance, wrong
mechanism" and R16 as "partly confirmed". **Both were misreads, and both are
retracted above.**

- R13: the criticism targeted the compressed v4 changelog phrase "house markers
  go to stdout" while the plan body (§3.9:678-680, :684) already draws the
  stdout/stderr distinction correctly. The body was right; the objection was
  aimed at the summary.
- R16: "hardcoded-`5` fixture" was read as a claim about existing test code. It
  is an instruction for the new v010 fixture. The absence of a hardcoded `5`
  in the suite is what the instruction calls for, not evidence against it.

The lesson worth carrying: this pass verified claims against the **plan body**,
but graded two findings against **prose summaries of it** — the same
summary-vs-spec confusion the third round correctly caught in round 2 (§4.2's
hand-written "15" versus the table's 21 rows). Grade against the artifact, not
its changelog.