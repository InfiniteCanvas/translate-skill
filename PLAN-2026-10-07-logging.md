# Plan — two-tier logging for `novel-translator` (v3)

**Date:** 2026-10-07
**Status:** proposal, not yet implemented. Owner decisions of 2026-10-07 are
locked in §1.2. **Revised three times after adversarial audit** — rounds 1-2
in `AUDIT-2026-10-07-plan-logging.md` (F-numbers F1-F18 below are the
independent verifier's; C/H/M/L numbers are the self-audit's), round 3 run as
three per-workstream adversarial agents against HEAD `3bbe4af` (R-numbers in
the v4 changelog).
**Author scope:** `D:\Repos\vibed\translate-skill` @ HEAD `3bbe4af`.

## v5 changelog

**v5 — closes the chapter-identity cluster (R1, R2) with measured evidence,
after a throwaway spike over the real `CHAPTER_RE`:**

- **R2's `(number, suffix)` fix was insufficient and is replaced.** Measured
  over seven pathological source filenames through the real
  `project.CHAPTER_RE`: number-only yields **2** directories for 7 files,
  `(number, suffix)` yields **4**, stem-keyed yields **6**. The
  `(number, suffix)` rule still merges `Chapter_001.md` into `Chapter_0001.md`
  — and unlike the suffix case, that merge is **platform-independent**, because
  those are genuinely different filenames that coexist on any filesystem.
  Neither audit round flagged padding; both treated suffix as the only axis.
- **§3.1 now keys the chapter directory on the file stem**, verbatim when it is
  already filesystem-safe. `discover()` reads `source/` flat
  (`project.py:57-63`), so within a project `file` ↔ stem is **1:1 by
  construction** — it is the only key that is unique without the manifest.
- **The stem's one residual merge is case/extension variants**
  (`Chapter_0042.md` / `CHAPTER_0042.md` / `Chapter_0042.MD`), and it is
  **unreachable on a case-insensitive filesystem**: measured on this box, three
  writes of those three names produced exactly **one file on disk**, so
  `discover()` can never see more than one. Where it *is* reachable
  (case-sensitive FS), the pre-existing `draft/<stem>.state.json` and
  `notes/<stem>.json` collide identically — so logs behave exactly like the
  artifacts they sit beside instead of introducing a new bug class. Recorded as
  a documented platform assumption.
- **The manifest-aware `chapter_dir_name(entry)` helper is deleted.** The
  lookup direction is `spec → manifest → file → stem → directory`, and
  `parse_range` (`pipeline.py:268`) already resolves through the manifest — so
  the CLI gets the exact filename for free, `cmd_logs 7` needs no reverse map,
  and the logger receives an already-resolved key. R2's underlying worry (a
  logger that cannot see the manifest) dissolves.
- **Bonus finding, not yet decided:** `tag` (`pipeline.py:900`) and
  `_chapter_subject` (`pipeline.py:1546`) are both number-only and both drop the
  suffix, so `Chapter_0042a.md` and `Chapter_0042b.md` print identical console
  tags and commit identical subjects today. Pre-existing, independent of
  logging, but the log directory would be `Chapter_0042a` while every console
  line says `[Chapter_0042]`.
- **The sanitization/hash fallback is deleted as dead code.** `discover()`
  admits a file only on a `CHAPTER_RE` match, and that regex admits only
  `Chapter_<1-4 digits><optional lowercase letter>.md` — so every reachable
  stem is already filesystem-safe, and `references/ingestion.md:41-45` states
  that near-miss names are silently ignored. The chapter key is
  `Path(file).stem`, verbatim: no charset test, no slug, no hash.
- **Scope check on the whole cluster:** a letter suffix marks
  extras/bonus chapters and is documented in `SKILL.md:38-39`,
  `README.md:34-36`, `references/ingestion.md:35-36`, and
  `references/file-formats.md:41-46`, and is persisted as a manifest field
  (`project.py:261-265`) — so R1 was reachable, not hypothetical. Mixed padding
  is schema-legal (`SKILL.md:39`, `ingestion.md:32-34`) but is a user mistake,
  and the stem separates it for free. Case variants cannot coexist on a
  case-insensitive filesystem (measured: three writes, one file).
- **Superseded in part by the `CHAPTER_RE` tightening (shipped 2026-10-07).**
  `project.CHAPTER_RE` is now `^Chapter_([0-9]{4})([a-z]?)\.md$` — padding is
  fixed at exactly 4 digits, so `Chapter_001.md` no longer matches and the
  **padding axis above is closed at the source**. The stem key is still
  required (suffix is reachable through documented usage, and case variants
  are reachable on case-sensitive filesystems), and the table above is kept as
  the historical record of why `(number, suffix)` was not enough. The
  tightening also added `project.near_miss_reason()` / `ignored_chapters()`
  and `[warn]` lines in `init`/`sync` (`tests/test_near_miss.py`, 32 checks),
  so a name that stops matching is reported instead of vanishing — the
  "silent" half of this finding is now handled by the tool rather than by the
  log layout. `migrations/v010.py` ships the rename of existing projects'
  files to the new form (carrying `chapters.json`, `story_state.json` and the
  per-chapter artifacts with it, and deliberately deferring suffixed chapters
  to an agent).

---

## v4 changelog

**v4 — round-3 audit (three per-workstream adversarial agents); no owner
decision overturned:**

- **R1 (critical) — suffixed manifest entries collide.** `CHAPTER_RE`
  (`project.py:18`) keeps `number` and `suffix` separate, and suffixed files
  are supported input sorted by `(number, suffix)` (`project.py:64`):
  `Chapter_0042a.md` and `Chapter_0042b.md` share `number == 42`, so §3.1's
  bare `Chapter_{number:04d}` would silently merge two chapters' logs — the
  exact failure M4 claimed to prevent, shipping green under §4.1's old check.
  Directories now key on `(number, suffix)`.
- **R2 — the `chapter=` value space was triple-inconsistent** (manifest
  filename vs the bracketed `tag` at `pipeline.py:900` vs the directory name,
  differing in padding, case, brackets, extension, and suffix), and the
  logger — which never sees the manifest — had no specified way to map it.
  A shared `chapter_dir_name(entry)` helper computes the canonical directory
  string once, in the manifest's scope; every site threads that string and
  the logger uses it verbatim. The unreachable stem+hash fallback is deleted.
- **R3 — the crash-path `chapter_end` had no implementable home.** `run_range`'s
  except has only `file`; `translate.main`'s KeyboardInterrupt handler has no
  chapter identity ("where possible" was vacuous); the index writer was
  unnamed; and `run_chapter`'s `"skipped"` return (`pipeline.py:903-905`) was
  missing from the model, so every skip would have left a false
  open-without-close crash signal. §3.6 now names the site, the best-effort
  payload, and the skip behavior.
- **R4 — `index.jsonl` under `log_orchestration: false` was still undecided**
  (v1's L3, never resolved). Index lines are now written unconditionally of
  both gates.
- **R5 — `logs/project/index.jsonl` had no writer** although §8.2 requires it
  and `--list`/`--run` need a registry for chapter-less invocations. The
  `run_start`/`run_end` emitters append its lines.
- **R6 — §4.6 under-reported existing-suite breakage:** `case_4_two_projects`'
  helpers glob `llm-*.jsonl` (`test_logger.py:74`, `:83`, `:185-197`) and
  would fail despite §3.4's "passes without modification" (now scoped to its
  two `_run_path` assertions); `test_cleanup_flow`'s trace reader globs
  `llm-*.jsonl` at `:249`; §4.6's `test_consensus` rationale was wrong (its
  calls are chapter-less → `logs/project/`).
- **R7 — the never-raises contract was only delivered for `OSError`.** The
  rewritten `log_event` adds serialization and cache logic that can raise
  `TypeError`; it now catches `Exception`, with a §4.1 check.
- **R8 — two dueling gate points** (the hook's `cfg` read vs `log_event`'s
  resolved flags, which disagree in test sandboxes with no `config.json`).
  Gating is centralized in `log_event`; the hook reads no config.
- **R9 — `chapter_end`/`run_end`'s `calls`/`tokens`/`elapsed_s` had no source
  anywhere,** and the reset didn't clear them (six test sites would leak
  counts). Per-(project, chapter) counters now live in the logger cache.
- **R10 — `tn_recheck`'s `do_chat` closure is built once before the loop**
  (`tn_recheck.py:95-100`, called at `:226`), so threading is a protocol
  decision: a per-iteration rebind that leaves the documented `chat` override
  contract untouched; `story._generate` gains a `chapter` parameter shared by
  both callers.
- **R11 — `degraded` sites enumerated** (consensus `:142-145`, `:172-176`;
  glossary-cleanup `pipeline.py:711-713`, which gains the `chapter` param);
  per-candidate failure decided as `llm_call`'s `error` field, not `degraded`.
- **R12 — `run_start`/`run_end` move from `translate.main`** (a dispatcher
  with no config/manifest/counts in scope) to the cmd_ layer; payload adapted
  per command family; `ping` excluded with a reason.
- **R13 — logs CLI underspecifications pinned:** `--json` stdout purity
  (house markers go to stdout), `--last` scope and ordering, `--run`/`--list`
  cross-bucket run_id dedupe, `--io` best-effort over written bodies, the
  exit-1 string made normative.
- **R14 — `report.md` data source pinned** (root tier-1 file filtered by
  `run_id` + chapter), header-only under `log_orchestration: false`,
  per-invocation timeline for resumed chapters, directory-scan coverage.
- **R15 — the doc-mirror list violated AGENTS.md's "every file schema":**
  `file-formats.md` gains a `logs/` schema section (run-file tiers, both
  index line shapes, `report.md` structure); README's schema enumeration,
  `maintenance.md`'s intro list, and SKILL.md's slot are named.
- **R16 (low) —** `llm_call` accounting for failed calls (`usage: null`
  counts as one call, zero tokens), §3.5's guard wording (ASSEMBLE /
  TRANSLATE guards differ), legacy `llm-*.jsonl` excluded from
  `--list`/`--run`, `test_migrate` stale head-version comments and a
  hardcoded-`5` fixture.

---

## v3 changelog

**v3 — driven by the independent audit, which overturned one core v2 design
decision:**

- **The ContextVar chapter scope is gone (F2, F3, H5, L5).** Chapter identity
  is now threaded explicitly as a `chapter=` kwarg through `consensus.chat` and
  `pipeline._chat`. This deletes the scope-leak hazard (H5), the house-style
  deviation (L5), the `tn` gap (F2), and the ambient-scope recap problem (F3)
  *at the root* rather than patching each. The fan-out still works: the value
  is captured in the `_trace_hook` closure, which the main thread builds before
  `pool.submit` — the same non-obvious property, now with an explicit value.
- **`run_id` is computed once per resolved project (F4)** and every path
  derives its filename from that stored string. v2 asserted the joinability
  property but never said where the value lived; a per-path stamp would have
  drifted a second between tier 1 and chapter 3's tier-2 file and silently
  broken every cross-tier query.
- **`logs/project/` gets a retention rule (F6).** v2 created the bucket and
  scoped retention to the root and to chapter dirs, leaving project-bucket
  files to accumulate forever.
- **`chapter_end` is emitted on the raise path too (F13)**, with
  `outcome: "crashed"`.
- **`report.md` is bounded and single-writer (F14).** `chapter_end` writes the
  metadata-only form; only `translate logs --report` writes bodies. Model
  output is HTML-escaped so a `</details>` in a reply cannot corrupt the
  structure. `lib/logreport.py` inherits the never-raise contract.
- **Flag gating is stated exactly (F1).** `log_llm` gates the two model lines
  only; `result`/`chunk`/`feedback` are unconditional, so v2's "no tier-2
  files at all" was false and is replaced.
- **Citations corrected (F9, F15, F16, F18):** six reset sites in three files
  (not three), eleven call sites (not twelve), `parse_range` raises
  `PipelineError` not `CliError`, the `read_log_events` glob is `llm-*.jsonl`
  at `:124`.
- **The `chmod` test is replaced (F17).** The verifier ran it: on this NTFS box
  `os.chmod(dir, 0o500)` yields mode `0o40555` and writes still succeed, so the
  check passed vacuously. It now induces a real `OSError`.
- **`test_logger.py` moves into "existing suites to update" (F10)** — two of its
  existing cases assert against the old layout and will fail.
- **Two doc-mirror rows added (F12):** the § Exit codes table and the README
  `translate logs` workflow, both mandated by `AGENTS.md`.
- **Carried from v2 (unchanged in substance):** `tn_recheck` and
  `glossary_cleanup` are tier 1 (H1, H2); `index.jsonl` is excluded from
  pruning (H3, F7); the reset mechanism is specified (H4, F5); the eight stage
  emission sites are specified (M1); `v011` is hand-written in the `v009`
  shape (C3, F11); chapter dirs key on the manifest number (M4); flags resolve
  once per run (M3); the acceptance criterion is parse-based, not grep (L1).

---

## 1. Goal and binding constraints

### 1.1 Goal

Today every event an invocation produces lands in one file, so a ten-chapter
run yields one log and chapter identity survives only as a field on a few
events. The owner wants the log scope split in two:

1. **Orchestration** — where the pipeline work is visible: run lifecycle,
   chapter lifecycle, stage transitions, gate verdicts, degradations.
2. **A full translation pipeline run for a chapter**, with model inputs and
   outputs logged — per chapter, not per process.

### 1.2 Owner decisions (locked 2026-10-07)

| Fork | Decision |
|---|---|
| Per-chapter log shape | Per `(chapter, invocation)` files plus `index.jsonl` |
| Human-readable surface | Both a per-chapter `report.md` **and** a `translate logs` CLI |
| Prompt-body verbosity | Separate `log_prompt_bodies` toggle |
| Retention | Tier 1 keeps 10 runs; tier 2 keeps 3 runs per chapter |

### 1.3 Binding constraints

1. **Logging must never break the pipeline.** Every write path —
   `lib/logger.py` and the new `lib/logreport.py` — is best-effort and never
   raises. Today `log_event` catches only `OSError` (`logger.py:110`), but the
   rewritten body adds serialization, cache, and index logic that can raise
   `TypeError`/`ValueError` (one non-serializable payload value must not kill
   a run), so it catches `Exception` — the house pattern elsewhere in `lib/`
   — and `logreport.py` swallows any generator exception the same way.
2. **`logger._run_path = None` is a documented reset contract with six call
   sites in three files**: `tests/test_cleanup_flow.py:216`, `:319`, `:405`,
   `:447`; `tests/test_consensus.py:135` (inside `reset_globals()`); and
   `tests/test_logger.py:68`. All must keep working unchanged — §3.4.
3. **Quality outranks quota cost** (`AGENTS.md`). No model routing, fan-out,
   or `max_tokens` behavior changes here. Log volume is not a reason to trim
   anything the pipeline already sends.
4. **Config-key changes require a new migration** (`AGENTS.md`) —
   `scripts/migrations/v011.py`.
5. **Doc mirrors must ship in the same change** (`AGENTS.md`): see §9.
6. `logs/` is already gitignored and in the `vcs.py` core rules
   (`lib/vcs.py:32`), so new subdirectories need no gitignore work.

## 2. Evidence — the current logging surface

`lib/logger.py` is 111 lines and owns everything:

| Element | Location | Behavior |
|---|---|---|
| `log_event(project_dir, event)` | `logger.py:91` | Appends one JSON line to the active run file |
| Run file naming | `logger.py:103` | `logs/llm-<YYYYMMDD-HHMMSS>-<command>-<pid>.jsonl` |
| Stamp derivation | `logger.py:102` | `time.strftime` evaluated **at path-creation time**, per path |
| Project re-pointing | `logger.py:99` | A different resolved `project_dir` opens a fresh run |
| Retention | `logger.py:81-88` | Newest `log_llm_keep_runs` `llm-*.jsonl` by mtime, project-wide |
| Serialization | `logger.py:45` | One `_LOG_LOCK` for the whole body — the consensus fan-out logs from worker threads |
| `log_llm` gate | `consensus.py:64` | Gates **only** `llm_request`/`llm_response`; pipeline events always write |

Events currently in that single stream:

| Event | Emitter | Chapter-scoped? |
|---|---|---|
| `llm_request`, `llm_response` | `client.chat` via `consensus._trace_hook` | yes, when the chapter is known |
| `attempt`, `attempt_failed` | `pipeline.py:1073`, `pipeline.py:1529` | yes |
| `balance_advisory` | `pipeline.py:1311`, `pipeline.py:1341` | yes |
| `glossary_cleanup` | `pipeline.py:732` | yes |
| `tn_recheck` | `translate.py:1010` | **no** — one aggregate event for the whole command |
| `notes_review` | `review_notes.py:414` | no |
| `glossary_review` | `translate.py:1273` | no |
| `review_fix` | `translate.py:1135` | no |

Model IO already carries everything needed for a full exchange — `call_id`,
`job`, `candidate`/`candidates`/`consensus_for`, `url`, `model`, `params`,
`guided_json`, `prompt`, `response`, `finish_reason`, `usage`, `elapsed_s`,
`error` (`client.py:203-232`). **No change to `client.py` is required.**

**The eleven `consensus.chat` call sites** (F15 corrects v1's "twelve"): six
through `pipeline._chat` (`pipeline.py:607`, `:683`, `:1161`, `:1356`,
`:1398`, `:1437`), plus `story.py:168` and `tn_recheck.py:96`, plus
`profile.py:78`, `review.py:216`, `review_notes.py:260`.

## 3. Desired behavior

### 3.1 Layout

```
<project>/logs/
├── run-20261007-023812-translate-12345.jsonl      # tier 1: orchestration
├── project/                                       # model IO with no chapter
│   ├── run-20261007-021000-profile-12001.jsonl
│   └── index.jsonl
├── chapters/
│   ├── Chapter_0007/
│   │   ├── index.jsonl
│   │   ├── run-20261007-023812-translate-12345.jsonl  # tier 2: full model IO
│   │   └── report.md
│   └── Chapter_0008/
└── epub-build.log                                 # unchanged
```

**`run_id` (F4).** The identity string `YYYYMMDD-HHMMSS-<command>-<pid>` is
computed **once per resolved project**, at that project's first logged event,
and stored in the per-project cache entry. Every path — tier 1, every chapter's
tier 2, the project bucket — derives its filename from that stored string, and
every line carries it as a field. This is what makes the join property true:
today the stamp is evaluated at path-creation time (`logger.py:102`), so under
a multi-path cache a tier-1 file opened at `03:12:00` and chapter 3's tier-2
file opened at `03:12:01` would produce two different `run_id`s and silently
unjoinable logs.

**Chapter directory naming (M4, R1, R2, v5).** The directory is the chapter's
**file stem**, verbatim whenever the stem is already filesystem-safe:

```
logs/chapters/Chapter_0042a/
```

**Why the stem and not `(number, suffix)`.** `discover()` iterates `source/`
flat and admits only files (`project.py:57-63`), so within one project
`file` ↔ stem is **1:1 by construction**. The stem is the only identifier
that is unique *without* consulting a manifest. Measured over seven
pathological source filenames through the real `CHAPTER_RE`:

| Scheme | Distinct directories for 7 files | Still merges |
|---|---|---|
| `Chapter_{number:04d}` (v3) | 2 | everything sharing a number |
| `Chapter_{number:04d}{suffix}` (R2) | 4 | `Chapter_001.md` → `Chapter_0001.md` |
| stem-keyed (v5) | 6 | case/extension variants only |

The `(number, suffix)` rule does fix R1's suffix merge, but **padding is a
second axis and it is platform-independent**: `Chapter_001.md` and
`Chapter_0001.md` are genuinely different filenames that coexist on any
filesystem, and both are admitted (`CHAPTER_RE` accepts 1-4 digits,
`project.py:18`, and `project.py:12-13` documents both conventions in use).
Neither audit round caught this.

**The stem's one residual merge, and why it is acceptable.** `CHAPTER_RE` is
`IGNORECASE`, so `Chapter_0042.md`, `CHAPTER_0042.md`, and `Chapter_0042.MD`
all yield the stem `Chapter_0042`. Measured on this box: writing all three
produced **one file on disk**, so on a case-insensitive filesystem `discover()`
can never return more than one of them and the merge is unreachable. Where it
*is* reachable (a case-sensitive filesystem), the pre-existing
`draft/<stem>.state.json` (`pipeline.py:318`) and `notes/<stem>.json`
(`tn.py:106`) collide in exactly the same way — so logs behave like the
artifacts beside them rather than introducing a new class of bug. Recorded as
a documented platform assumption, not an oversight.

**It joins the existing artifact family.** One stem now greps a chapter's
state file, notes, dropped notes, and its logs:
`draft/<stem>.state.json` · `notes/<stem>.json` · `notes/<stem>.dropped.json` ·
`logs/chapters/<stem>/`. Making logs the only artifact keyed differently would
be a maintenance trap.

**The lookup direction removes the need for a manifest-aware helper (R2).**
`spec → manifest → file → stem → directory`: `parse_range` (`pipeline.py:268`)
already resolves a spec through the manifest, so `cmd_logs 7` gets the exact
filename for free and **never reverse-maps a directory back to a chapter**.
`lib/logger.py`, which never sees the manifest, uses the threaded `chapter=`
value (§3.3) verbatim as the directory name. v4's `chapter_dir_name(entry)`
helper is deleted.

**No sanitization, slugging, or hashing — it would be dead code (v5).** v5
initially added a safe-charset test plus a `slug--<sha256>[:6]` fallback for
"unsafe" stems. That guard is unnecessary and is removed: `discover()` admits
a file only when `CHAPTER_RE` matches (`project.py:60-61`), and that regex
(`^Chapter_([0-9]{1,4})([a-z]?)\.md$`, `project.py:18`) admits **only**
`Chapter_<1-4 digits><optional lowercase letter>.md`. Every reachable stem is
therefore already filesystem-safe — alphanumeric plus one underscore — and
`references/ingestion.md:41-45` states it outright: near-miss names
(`Chapter_0007.zh.md`, `chapter 7.md`, `0007.md`) are **silently ignored**,
producing no manifest entry. A stem that would need sanitizing cannot reach
the logger at all. The key is `Path(file).stem`, verbatim, full stop —
nothing more to get wrong.

**Suffixed names are real, not hypothetical (v5).** A letter suffix marks
extras/bonus chapters (`Chapter_0042a.md` sorting between `Chapter_0042.md` and
`Chapter_0043.md`) and is documented in four places — `SKILL.md:38-39`,
`README.md:34-36`, `references/ingestion.md:35-36`, `references/file-formats.md:41-46`
— and persisted as a manifest field (`project.py:261-265`). R1 described a
reachable scenario, not a hypothetical one.

**Padding is allowed by the schema but practically a user mistake.** Both
conventions "work" (`SKILL.md:39`, `references/ingestion.md:32-34`), so
`Chapter_001.md` and `Chapter_0001.md` could in principle coexist and mean the
same chapter — a mistake, not a convention. The stem separates them for free,
so it costs nothing to be correct here.

`run_chapter`'s `tag` at `pipeline.py:900` is `[Chapter_0042]` — bracketed and
suffix-less — so it is console display text, never the directory key; see the
v5 changelog for the open question about fixing it.

**Retention applies to all three buckets (F6).** v2 scoped pruning to the root
and to chapter dirs, leaving `logs/project/` to grow without bound. All three
are pruned:

| Bucket | Pruned by | Count |
|---|---|---|
| `logs/run-*.jsonl` | `log_llm_keep_runs` | 10 |
| `logs/project/run-*.jsonl` | `log_llm_keep_runs` | 10 |
| `logs/chapters/<N>/run-*.jsonl` | `log_chapter_keep_runs` | 3 |

The prune glob matches the `run-<run_id>.jsonl` name shape exactly and **never
`index.jsonl`** (H3, F7) — `index.jsonl` is append-only, so it is always among
the oldest files by mtime, which is precisely what "keep the newest N" deletes
first. Losing it destroys the crash signal it exists to carry. The glob also
never touches `epub-build.log`, `chapters/`, or legacy `llm-*.jsonl`.

**`index.jsonl` marks stale entries (M5).** Each index is the history of its
bucket (chapter directory, project bucket — §3.7) and is not itself pruned,
so `open`/`close` entries naming run files that retention has since removed
are kept and marked `files_pruned: true`. `translate logs` and `report.md`
skip such entries.

### 3.2 Tier split — each event belongs to exactly one tier

| Tier | Audience | Contents | File |
|---|---|---|---|
| 1 | "what happened in this run" | structural events only, never a prompt or a response | `logs/run-<run_id>.jsonl` |
| 2 | "what the models were given and said, for this chapter" | model IO and the pipeline's reading of individual model outputs | `logs/chapters/<Chapter_NNNN>/run-<run_id>.jsonl` |

The routing table is explicit and exhaustive, defaulting to tier 1:

| Event | Tier | Rationale |
|---|---|---|
| `run_start`, `run_end` | 1 | run lifecycle |
| `chapter_start`, `chapter_end` | 1 | run lifecycle |
| `stage` | 1 | stage transition |
| `gate` | 1 | gate verdict |
| `degraded` | 1 | non-fatal failure |
| `llm_call` | 1 | per-call metadata summary, no bodies |
| `attempt`, `attempt_failed` | 1 | already timeline events; kept, not replaced |
| `balance_advisory` | 1 | structural warning |
| `glossary_cleanup` | 1 | **pipeline summary** — which terms were retired/kept. Its judgment LLM call goes to tier 2 |
| `tn_recheck` | 1 | **command summary** — one aggregate event for N chapters, no `chapter` field |
| `notes_review`, `glossary_review`, `review_fix` | 1 | project-scoped maintenance |
| `llm_request`, `llm_response` | 2 | model IO |
| `result` | 2 | the pipeline's reading of one model output |
| `chunk` | 2 | per-chunk bounds and outcome |
| `feedback` | 2 | complete gate-reason history |

**The governing principle, so the table stays consistent:** tier 1 holds
anything that *summarizes a run, a chapter, a stage, or a command*; tier 2
holds model IO and the pipeline's reading of **individual** model outputs.
That principle is what makes the v1 misassignments visible —
`glossary_cleanup` and `tn_recheck` summarize, so tier 1 (H1, H2);
`result` and `feedback` describe one model output, so tier 2.

**Index lines are not routed events.** The `open`/`close` lines of §3.7 are
side-records appended by the `chapter_start` / `chapter_end` / `run_start` /
`run_end` emitters (R3, R5). They carry no model content and are written
**unconditionally of both gates** (R4): they are the tier-2 file's own
metadata, the crash signal, and the `translate logs` registry.

**Chapter-less model IO goes to `logs/project/run-<run_id>.jsonl`.** With
explicit `chapter=` threading (F2), a chapter-less call is one that passes
`chapter=None`: `profile.py:78`, `review.py:216`, `review_notes.py:260`.

An event routed to tier 2 with no chapter falls back to the project bucket.
**A tier-2 event is never written to tier 1**, so the timeline can never grow a
prompt by accident.

### 3.3 Chapter threading, and how the fan-out still works (F2, F3)

**Chapter identity is an explicit `chapter=` kwarg, not an ambient scope.**

`consensus.chat` and `pipeline._chat` each gain
`chapter: str | None = None` — the canonical directory string of §3.1, or
`None` — threaded into `_trace_hook`:

```python
def _trace_hook(project_dir, cfg, job, extra=None, chapter=None):
    # `chapter` is bound here, on the CALLING (main) thread.
    def hook(meta: dict) -> None:
        if meta.get("event") == "llm_response":
            logger.log_event(project_dir, {
                "event": "llm_call", "job": job, "chapter": chapter,
                **(extra or {}),
                "model": meta.get("model"),
                "usage": meta.get("usage"),
                "finish_reason": meta.get("finish_reason"),
                "elapsed_s": meta.get("elapsed_s"),
                "error": meta.get("error"),
            })
        logger.log_event(project_dir, {"job": job, "chapter": chapter,
                                       **(extra or {}), **meta})
    return hook
```

The hook reads **no config and applies no gate** (R8): both events go through
`log_event`, which routes them by tier and gates them against the per-project
resolved flags — `llm_call` under `log_orchestration`, `llm_request` /
`llm_response` under `log_llm` (with body stripping under
`log_prompt_bodies`). Two reasons to centralize: v1's defect was the hook
wrapping its whole body in the `log_llm` gate, which made the tier-1
`llm_call` unreachable with `log_llm: false`; and gating inside the hook from
the caller's `cfg` would create a second flag source that can disagree with
`log_event`'s resolved one — test sandboxes carry no `config.json`, so a
hand-built `cfg` and the resolved flags diverge.

**Why the fan-out still lands in the right chapter.** `consensus.py:112-118`
builds `_trace_hook(...)` on the main thread and only then calls `pool.submit`,
so `chapter` is bound before any worker runs. `ThreadPoolExecutor` does not
copy context to workers, and it does not need to: the value is already a
closure constant. This is the same non-obvious property the v2 ContextVar
relied on, now carrying an explicit value instead of ambient state.

**The eleven call sites resolve as:** every site passes the chapter's **stem**
(§3.1) — the string `run_chapter` already computes at `pipeline.py:901` — or
`None`, so the logger never re-derives anything.

| Site | `chapter=` | Note |
|---|---|---|
| `pipeline.py:1161` (translator) | `stem` | already in scope in `run_chapter` |
| `pipeline.py:1356` (FAITH) | `stem` | |
| `pipeline.py:1398` (GLOSSARY_EXPAND) | `stem` | |
| `pipeline.py:1437` (TN_GENERATE) | `stem` | |
| `pipeline.py:683` (glossary cleanup) | `stem` | `_cleanup_drift_signals` also gains a `chapter` parameter (R11) |
| `pipeline.py:607` (glossary merge) | `stem` | `_apply_glossary_proposal` takes `chapter_order` today; it gains a `chapter` parameter |
| `story.py:168` (recap) | the predecessor's / summarized chapter's stem | see below |
| `tn_recheck.py:96` (tn re-annotate) | that loop iteration's stem | see below |
| `profile.py:78` | `None` | |
| `review.py:216` | `None` | |
| `review_notes.py:260` | `None` | |

**Recap backfill is now correct by construction (F3, C2).**
`story.ensure_recap` (`story.py:208-234`) generates the recap for the
**predecessor**: it reads the predecessor's *translated* body
(`story.py:231`) and prompts `recap.md` with the predecessor's title and body,
while translating chapter N. Because the chapter is now explicit,
`_default_chat` receives whichever is true of the two and passes it:

- `ensure_recap`'s **state-hit** path (no LLM call) — nothing to file.
- The **backfill** path — the predecessor's canonical dir string, resolved
  from the predecessor's manifest entry (in scope at `story.py:219-234`), so
  the call lands in the **predecessor's** tier-2 file, next to that chapter's
  own work.
- `story.record_recap` at `pipeline.py:1501` — the chapter it summarizes.

Both paths route through the shared `_generate` (`story.py:174-200`), so
`_generate` gains a `chapter` parameter passed by each caller (R10). The
backfill's run file lands in the predecessor's directory under the **current
run's** `run_id`; the predecessor's index has no line for it, which is why
§3.10's report coverage scans the directory rather than the index.

This is strictly better than v2's label-the-misattribution compromise: the
backfill call now appears in the chapter it is actually about. v2's §4.2 check
("chapter 7's file contains no line tagged chapter 8") could not have caught
the defect, because the misfiled line was tagged with the scope's chapter.

**`tn` is now covered (F2, R10).** `cmd_tn` (`translate.py:1003`) calls
`tn_recheck.recheck_chapters` directly and never enters `run_chapter`, so an
ambient scope would have left every chapter's annotator prompt/response in the
project bucket with no `chapter` tag. The `do_chat` closure is built **once,
before the loop** (`tn_recheck.py:95-100`, called per iteration at `:226`), so
a loop value cannot be bound into it after the fact. The mechanism: the loop
(`for entry, file in eligible:`, `tn_recheck.py:173`) rebinds a
`chapter=`-carrying callable per iteration — e.g. a `partial` over the
internal `default_chat` — leaving the documented `chat: Callable[[str], str]`
override contract (`tn_recheck.py:79`) and the test stubs that rely on it
untouched.

### 3.4 The reset contract and the two gates (H4, F5, C1)

**Reset (H4, F5).** v2 said "keep `_run_path` as the documented alias and
clear both", which is not implementable: a bare module assignment executes no
code and cannot clear a dict. The mechanism is specified instead:

- `_paths` is the cache: `{(tier, chapter): Path}` per resolved project.
- **`_run_path` retains a precise meaning: the active tier-1 path of the most
  recently logged project**, exactly as today. This keeps
  `test_logger.py`'s two `_run_path` assertions in `case_4_two_projects`
  (`:202-203`, `:212-213`) passing without modification — the case's
  `llm-*.jsonl` helpers are another matter and are rewritten in §4.6 (R6).
- `log_event` **reads `_run_path` on every call**. When it is `None`, the module
  clears `_paths` entirely — including the call/token counters below — and
  resolves fresh. That is what makes the plain
  assignment `logger._run_path = None` — used verbatim at six sites — a
  working reset for both tiers.
- **Call/token counters (R9).** Each project's cache entry also holds
  per-`chapter` `calls`/`tokens` counters and a chapter-start timestamp.
  `log_event` increments them for every `llm_call` it sees **before gating**,
  so they accumulate even with `log_orchestration: false`. `chapter_end` and
  `run_end` consume them read-and-reset — they are the only specified source
  for those payload fields (§3.7), including on the crash path (§3.6). The
  reset clears them, so the six test-site resets cannot leak counts across
  runs.

**The two gates (C1, R8).** Gating lives in exactly one place: `log_event`,
applied to each event by its routed tier from the per-project resolved flags —
tier-1 events require `log_orchestration`; `llm_request`/`llm_response`
require `log_llm` (plus body stripping under `log_prompt_bodies`);
`result`/`chunk`/`feedback` and index lines are unconditional. The hook emits
unconditionally into `log_event` (§3.3). Under the current code
`consensus.py:64-68` computes `enabled` from `log_llm` once and wraps the
*entire* hook body, so a tier-1 `llm_call` emitted from inside it would be
suppressed too — v1's promised property was unreachable; v3's fix of reading
the flags inside the hook from the caller's `cfg` would have created a second
flag source that disagrees with `log_event`'s in any sandbox without a
`config.json`.

### 3.5 Stage and gate emission sites (M1)

`stage` events are emitted at **eight** sites. Seven coincide with `advance()`
(`pipeline.py:1022`), which already persists `state["stage"]`:

| Stage | `advance()` site | Notes |
|---|---|---|
| TRANSLATE | — | **entered at `pipeline.py:1070` with no `advance()` call**; needs its own emission before the chunk loop |
| VALIDATE | `:1277` | |
| BALANCE | `:1296` | |
| FAITH | `:1347` | |
| GLOSSARY_EXPAND | `:1377` | |
| TN_GENERATE | `:1427` | |
| TN_DEDUP | `:1450` | |
| ASSEMBLE | `:1475` | |

Each block is guarded by `failed_stage is None and start_idx <= _STAGE_IDX[...]`
— except ASSEMBLE, guarded by `failed_stage is None` alone (`pipeline.py:1473`),
and TRANSLATE, whose `:1070` guard checks only `start_idx` (`failed_stage` is
always `None` there).
Two consequences the implementation must respect:

- **`elapsed_s` requires wrapping each stage block.** The wrapper emits
  `stage begin` after the guard passes and `stage end` in a `finally` around
  the block only, without disturbing the resume logic.
- **A resumed chapter emits only the stages it actually enters.** A chapter
  resuming at BALANCE never reaches the TRANSLATE site, so §4.3 states the
  "8 stages" check for a *fresh* chapter and pairs it with a check that a
  resumed chapter emits the reduced set and no TRANSLATE event.

`gate` is emitted where `failed_stage` is assigned — VALIDATE at
`pipeline.py:1293`, FAITH at `pipeline.py:1369` and `:1374` — carrying the
**complete** reason list. Today `attempt_failed` (`pipeline.py:1529`)
truncates feedback to `state["feedback"][-3:]`.

`run_start` / `run_end` are emitted from the **cmd_ layer** of every
subcommand that does model work (`translate`, `retry`, `tn`, `review`,
`profile`) — not from `translate.main`, which is a thin dispatcher
(`translate.py:1875-1905`) with no config, manifest, or results in scope; the
per-outcome chapter counts exist only in `run_range`'s return, consumed by
`cmd_translate`/`cmd_retry` (`translate.py:923-930`, `:968-975`) (R12).
`run_end`'s `chapters` field is filled for `translate`/`retry` and omitted
for `tn`/`review`/`profile`, which have no chapter outcomes. `ping` is
excluded: its probe (`translate.py:577`) bypasses `consensus.chat`, so its
`run_end` could only report fabricated zeros.

`chapter_start` is emitted at `run_chapter` entry, **after** the skip
decision (`pipeline.py:903-905` can return `"skipped"`), so a skipped chapter
leaves no index lines and no false open-without-close signal (R3).

`degraded` is emitted at three sites (R11): consensus single-survivor
(`consensus.py:142-145`), consensus call failed (`:172-176`), and
glossary-cleanup failure (`pipeline.py:711-713`; `_cleanup_drift_signals`
gains the `chapter` parameter, as `_apply_glossary_proposal` does). A
per-candidate failure (`consensus.py:135-139`) is **not** `degraded` — it is
visible in `llm_call`'s `error` field.

### 3.6 The crash path (F13, R3)

`run_range` catches **any** `Exception` from `run_chapter` and continues to
the next chapter (`pipeline.py:1571-1582`), marking it `needs-review` itself;
`KeyboardInterrupt` is not caught and aborts the run (`:1589-1592`).
Consequently:

- **Emission site.** `run_chapter`'s body is wrapped in
  `try/except Exception`, which emits `chapter_end` with `outcome: "crashed"`
  and the exception type and message, then **re-raises** — `run_range`'s
  behavior is unchanged. The except block holds `state` (attempts, stage);
  `calls`, `tokens`, and `elapsed_s` on this path are **best-effort** from the
  logger-cache counters and the `chapter_start` timestamp (§3.4) — a crash
  may have cut aggregation short, and the payload reports what it knows.
- The `index.jsonl` `close` line is written with `outcome: "crashed"` by the
  same emitter, so the crash is recorded even though no normal-path
  `chapter_end` ran. The writer is the `chapter_start`/`chapter_end` emitter
  itself (§3.7).
- With explicit `chapter=` threading there is no scope to leak — the previous
  concern (H5) disappears with the ContextVar.
- `KeyboardInterrupt` emits nothing: `translate.main`'s handler
  (`translate.py:1890-1902`) has no chapter identity, attempts, or counters in
  scope, so v3's "where possible" was vacuous — and with a live fan-out it
  hard-exits via `os._exit(130)` (`translate.py:1898-1901`, gated on
  `consensus._ACTIVE_FANS`; in-process test callers keep the ordinary
  `return 130`), so nothing after it can run anyway. The
  `open`-without-`close` index line is the signal, as designed.

### 3.7 Event schema

Every line carries `ts` (UTC, millisecond precision, as today), `run_id`, and
`chapter` where known.

**Tier 1 — new events:**

| Event | Payload |
|---|---|
| `run_start` | `command`, `argv`, `provider_jobs` (job → model list; **no keys, no auth**), `git_head`, `manifest` counts |
| `run_end` | `outcome`, `chapters` (counts per outcome), `calls`, `tokens` per job, `elapsed_s` |
| `chapter_start` | `file`, `number`, `lines`, `resume_stage`, `resume_chunks`, `force` — emitted at `run_chapter` entry after the skip decision (§3.5) |
| `chapter_end` | `file`, `outcome` (`translated`\|`needs-review`\|`crashed`), `attempts`, `stages_run`, `calls`, `tokens` per job, `elapsed_s`, `error` when crashed |
| `stage` | `chapter`, `stage`, `attempt`, `phase`, `elapsed_s` |
| `gate` | `chapter`, `stage`, `verdict`, `reasons` — **all** reasons |
| `degraded` | `chapter`, `where`, `reason` |
| `llm_call` | `chapter`, `job`, `model`, `candidate`/`consensus_for`, `usage`, `finish_reason`, `elapsed_s`, `error` — **no bodies** |

**Tier 2 — existing `llm_request`/`llm_response` unchanged, plus:**

| Event | Payload |
|---|---|
| `result` | `call_id`, `job`, `kind`, `parsed` summary — verdict, terms proposed/applied, notes kept/dropped, lines accepted/rejected, truncation repair |
| `chunk` | `chapter`, `index`, `total`, `lo`, `hi`, `max_tokens`, `attempt`, `outcome` |
| `feedback` | `chapter`, `stage`, `reasons` (complete history across attempts) |

**`llm_call` accounting (R16).** One `llm_call` per `client.chat` invocation
that reached the hook. A failed call still fires `llm_response` with `error`
set and `usage: null` (`client.py:246-314`), so it counts as one call
contributing zero tokens to every sum; a failure before the hook is built
(`resolve_model`, `client.py:163`) produces no lines at all.

**Index writers (R4, R5).** The chapter directory's `index.jsonl` gets an
`open` line at `chapter_start` and a `close` line at `chapter_end` (crash
path included); the project bucket's `index.jsonl` gets an `open`/`close`
pair per invocation from the `run_start`/`run_end` emitters — which gives
`--list`/`--run` a registry for chapter-less invocations (`profile`,
`review`) and is what §8.2's "one project-bucket `index.jsonl`" means. Both
are append-only and **unconditional of both gates** (§3.2):

```json
{"ts": "...", "run_id": "...", "phase": "open",  "command": "translate", "pid": 12345, "file": "Chapter_0007.md"}
{"ts": "...", "run_id": "...", "phase": "close", "outcome": "translated", "attempts": 1, "stages": 8, "calls": 11, "tokens": {}, "elapsed_s": 412.7}
```

(The project bucket's `open` line omits `file`.) An `open` line with no
matching `close` marks the invocation that died.

### 3.8 Config keys and exact gating (F1, M3)

| Key | Default | Gates **exactly** | Change |
|---|---|---|---|
| `log_orchestration` | `true` | tier-1 structural events **and** the tier-1 `llm_call` summary | **new** |
| `log_llm` | `true` | `llm_request` / `llm_response` **only** | existing, re-scoped |
| `log_prompt_bodies` | `true` | the `prompt` / `response` fields inside those two lines | **new** |
| `log_llm_keep_runs` | `10` | retention for the `logs/` root **and** `logs/project/` | existing, default raised from 5 |
| `log_chapter_keep_runs` | `3` | retention per chapter directory | **new** |

**`result`, `chunk`, and `feedback` are unconditional.** They are the
pipeline's own small structural record of a chapter, and they are what you want
most when model logging is turned off for cost. Consequence, stated plainly
because v2 got it backwards: **`log_llm: false` does not remove tier-2 files.**
A chapter's tier-2 file still exists with `chunk` / `result` / `feedback` lines
and simply carries no model exchange.

The properties that hold at each setting:

| Setting | Tier 1 | Tier 2 |
|---|---|---|
| all defaults | full timeline | full model IO with bodies |
| `log_llm: false` | full timeline **including `llm_call` per call** | `chunk` / `result` / `feedback` only |
| `log_prompt_bodies: false` | unchanged | model lines keep `finish_reason`, `usage`, `elapsed_s`, `error`; gain `prompt_chars` / `response_chars` |
| `log_orchestration: false` | no tier-1 file; both indexes still written; `chapter_end`'s report is header-only | unchanged (it is the chapter's own trace) |

**Flags resolve once per run, not per event (M3).** Today `_keep_count`
(`logger.py:72`) reads `config.json` once per run-file creation. Reading three
flags per event across ~50 events per chapter would be a real regression. The
first `log_event` for a project resolves all flags into that project's cache
entry; later events reuse it. Body stripping happens in `log_event` at write
time, so one place covers all eleven call sites without touching them.
Gating is likewise centralized there (R8): `log_event` applies every flag
against the routed tier (§3.4). No emitter and not the trace hook reads
config.

### 3.9 CLI

```
translate logs [SPEC] [--run RUN_ID] [--list] [--io|--no-io] [--json] [--last N] [--report]
```

- `SPEC` reuses `pipeline.parse_range`, so `logs 7`, `logs 1-5`, and
  `logs Chapter_0007.md` all resolve like `translate`/`retry`/`mark`.
- Bare `logs` prints the newest run's tier-1 timeline.
- **`--last N`** prints the newest `N` runs, default `1`, newest first.
  `--last 0` is a usage error. With a SPEC the runs come from that chapter's
  `index.jsonl`, ordered by `open` timestamp (stale entries skipped); without
  a SPEC, from the root tier-1 bucket by mtime (the retention precedent).
  Bare `logs` is `--last 1` without a SPEC (R13).
- **`--run RUN_ID`** matches a run id exactly or by unique prefix. Since a run
  id embeds a `<pid>` the user cannot know, an ambiguous or unmatched value
  exits 2 and lists the available run ids with commands and timestamps.
- **`--list`** prints available run ids (command, start time, chapter count).
- Both `--run` and `--list` scan **all three buckets** and dedupe by `run_id`
  (R13): one id spans a run's tier-1, tier-2, and project files, and
  selecting it selects the whole run — a literal per-file listing would call
  every multi-chapter run ambiguous. The root bucket has no index, so its
  metadata comes from the filename; project-bucket runs show chapter count
  `0`. Legacy `llm-*.jsonl` files are neither listed nor matched.
- `--io` includes prompt/response bodies; `--no-io` prints metadata only.
  Default follows `log_prompt_bodies`. `--io` is best-effort over what was
  **written** (R13): calls logged with `log_prompt_bodies: false` carry only
  `prompt_chars`/`response_chars` and appear metadata-only.
- `--report` regenerates `report.md` (§3.10) for the matched chapters.
- `--json` emits one JSON object per event on stdout — **only** JSON objects
  (R13): the house `[ok]`/`[warn]` markers print to stdout elsewhere
  (`translate.py:922` and kin), so they are suppressed in this mode.

Console markers follow the house style (`[ok]`/`[warn]`/`[FAIL]`). The
exit-1 line is normative (R13): `[FAIL] no logs for spec: {spec} (valid
chapters: {lo}-{hi})`, printed via `_fail` (stderr, `translate.py:64-65`).
`[ok] Chapter_0007: 3 runs, 11 calls` is illustrative.

**Exit codes** (`references/file-formats.md` § Exit codes):

| Exit | Condition |
|---|---|
| 0 | logs found and printed |
| 1 | no logs matched the spec — matching the `glossary search` / `tn` precedent |
| 2 | usage error (F16): an unmatched/ambiguous `--run`, `--last 0`, or an unmatched spec. Note `parse_range` raises `pipeline.PipelineError` (`pipeline.py:306`), which `translate.main` also maps to exit 2 (`translate.py:1884-1886`) — the test asserts the **exit code**, not the exception type |

### 3.10 `report.md` (F14)

**Single writer, bounded work, escaped content.**

- **`chapter_end` writes the metadata-only report.** Header, stage timeline,
  gate verdicts, and the call table — no `<details>` blocks, no prompt or
  response bodies. Data source (R14): the root tier-1 file filtered to the
  current `run_id` + chapter — bounded work on the pipeline's critical path,
  never the whole chapter history. For a resumed chapter the stage timeline
  covers the stages entered in this invocation. With `log_orchestration:
  false` there is no tier-1 file to read: `chapter_end` still writes the
  index `close` line and a **header-only** report.
- **`translate logs --report` regenerates it**, and `--report --io` produces
  the full form with every retained run's bodies inside `<details>`. Bodies
  exist only for calls logged with `log_prompt_bodies: true`; stripped calls
  appear metadata-only (R13). That is the only path that reads the chapter's
  whole retained history, and it runs on demand, never mid-pipeline.
- Both writes use `project.atomic_write_text`, so the later writer replaces the
  earlier one cleanly and a reader never sees a half-written report.
- **Model output is HTML-escaped** (`&`, `<`, `>`) before interpolation. A
  reply containing `</details>` or a fence would otherwise corrupt exactly the
  structure §4.5 asserts on.
- **`lib/logreport.py` inherits the never-raise contract** (§1.3 constraint 1):
  a generator failure logs a `[warn]` and leaves the previous report in place,
  and never propagates into the pipeline.
- Entries whose run files were pruned are listed as such (M5), and
  `index.jsonl` / `report.md` are excluded from the "retained run files" scan.
  Coverage is a **directory scan** of the chapter's `run-*.jsonl` files (R3):
  the recap backfill writes a run file into the predecessor's directory that
  the predecessor's index never records, so the index alone undercounts;
  index entries enrich the listing with stale marks.

Sections: header (chapter, number, line count, outcome, attempts, runs covered)
· stage timeline · gate verdicts with every reason · call table (job, model,
candidate, tokens in/out, elapsed, finish reason) · per-call detail in
`<details>` (full form only).

## 4. What to test for

House convention: every script is a self-contained PASS/FAIL program with a
docstring, a `check(name, cond, detail)` helper, a `N passed, M failed` summary
line, and exit 1 on any failure. `tests/run_all.py` parses that summary and
fails a script reporting zero checks, so a new suite must not report 0.

### 4.1 `tests/test_logger.py` (extend — see also §4.6)

| Check | Pins |
|---|---|
| `log_event` tier-1 event lands in `logs/run-<run_id>.jsonl` | new naming |
| `log_event` tier-2 event with a chapter lands in `logs/chapters/Chapter_NNNN/run-<run_id>.jsonl` | tier-2 path |
| tier-2 event with no chapter lands in `logs/project/run-<run_id>.jsonl` | project bucket |
| an unknown event name defaults to tier 1 | routing default |
| every line carries `ts` and `run_id` | joinability |
| `log_prompt_bodies: false` drops `prompt`/`response`, keeps `prompt_chars`/`response_chars`, `finish_reason`, `usage`, `elapsed_s` | body toggle |
| flags are read from `config.json` **once per run**, not once per event | M3 |
| `_prune` keeps the newest `log_llm_keep_runs` `run-*.jsonl` at the root | tier-1 retention |
| **`logs/project/` is pruned to `log_llm_keep_runs` too** | F6 |
| tier-2 prune keeps the newest `log_chapter_keep_runs` per chapter dir, independently per chapter | per-chapter retention |
| **after 4 runs in one chapter dir with `log_chapter_keep_runs: 3`, `index.jsonl` still exists** | H3, F7 |
| `_prune` never touches `epub-build.log`, `index.jsonl`, `chapters/`, or legacy `llm-*.jsonl` | blast radius |
| legacy `llm-*.jsonl` are neither pruned nor read | history preserved |
| **`run_id` is identical across the tier-1 file and every chapter's tier-2 file of one run** | F4 |
| `_command_tag` unchanged: skips `--project DIR` in both spellings, sanitized, truncated to 24, `run` fallback | existing contract |
| `_keep_count` unchanged: int-coerced, clamped ≥ 0, `DEFAULTS` on any read failure | existing contract |
| **`logger._run_path = None` clears every cached path and self-heals into fresh runs for both tiers** | H4, F5 |
| `_run_path` still holds the active **tier-1** path of the most recent project | keeps `case_4_two_projects` valid |
| the chapter directory is the file **stem**, verbatim when it matches `^[A-Za-z0-9._-]{1,64}$` — matching `draft/` and `notes/` | M4, R1, R2, v5 |
| **the stem scheme is injective over the pathological set**: `Chapter_0042.md`, `Chapter_0042a.md`, `Chapter_0042b.md`, `Chapter_001.md`, `Chapter_0001.md` yield five distinct directories | R1 suffix + v5 padding |
| two manifest entries differing only in case/extension yield the same directory — and cannot coexist on a case-insensitive filesystem, matching `draft/` and `notes/` | v5 documented assumption |
| an unsafe stem (`Chapter 4: the start.md`) **cannot reach the logger** — `CHAPTER_RE` rejects it, so `discover()` yields no manifest entry; the key stays a bare `Path(file).stem` with no sanitization or hashing | v5 dead-code removal (`ingestion.md:41-45`) |
| two project dirs in one process each keep their own tier-1 and tier-2 files | project isolation |
| 50 concurrent `log_event` calls from worker threads produce 50 well-formed lines with no interleaving | fan-out safety |
| **writing where `logs/` is a regular file raises no `OSError` out of `log_event`** | F17 — replaces the `chmod` check, which passes vacuously on NTFS (`os.chmod(dir, 0o500)` yields mode `0o40555` and writes still succeed; verified empirically on this box) |
| a payload value `json.dumps` cannot serialize (e.g. a `Path`) is swallowed — nothing leaves `log_event` | R7 |
| with `log_orchestration: false`, no tier-1 file is created, both `index.jsonl` files still receive their lines, and the counters still accumulate (the next `chapter_end` reports non-zero calls) | R4, R8 |

### 4.2 `tests/test_log_routing.py` (new)

Drives the routing table through the real emitters rather than synthetic event
names. The event list is **table-driven off §3.2's own rows (21)**, so the
check cannot drift from the table the way a hand-written "15" did (F8).

| Check | Pins |
|---|---|
| each of the 21 named events reaches its specified tier | the table is exhaustive and correct |
| `tn_recheck` lands in tier 1 despite the tier-2-adjacent name | H1 |
| `glossary_cleanup` lands in tier 1 | H2 |
| a real `consensus.chat` fan-out writes every candidate line to the passed chapter's tier-2 file | closure capture |
| the consensus synthesizer line lands in the same chapter file | fan-out correctness |
| **after `translate tn 1-3`, each chapter's tier-2 dir holds that chapter's annotator calls and none holds another's** | F2 |
| **a recap backfill for chapter N-1 lands in chapter N-1's tier-2 dir, not N's** | F3, C2 |
| **with `log_llm: false`, tier 1 still contains one `llm_call` per `client.chat` invocation — failed calls included, `usage: null` — and chapter tier-2 files exist with `chunk`/`result`/`feedback` but no `llm_request`** | C1, F1, R16 |
| a chapter-less job (`profile`) writes to `logs/project/` and leaves `chapters/` empty | project bucket |
| `chapters/` contains exactly one directory per chapter translated in the run | no cross-talk |
| `Chapter_0007`'s tier-2 file contains no line tagged `Chapter_0008` | chapter isolation |
| **every `run_id` in the tier-1 file equals every `run_id` in both chapter tier-2 files** | F4 |
| tier 1 contains zero lines carrying a `prompt` or `response` **key** — checked by parsing JSON, not grepping | tier contract, L1 |
| `index.jsonl` gets an `open` line per chapter-invocation and a `close` line at `chapter_end` | index contract |
| a killed process leaves an `open` line with no `close` | crash visibility |
| `run_end` token totals equal the sum of tier-1 `llm_call` usage (`usage: null` contributing 0) | accounting integrity, R16 |

### 4.3 `tests/test_pipeline_log_events.py` (new)

Drives `run_chapter` against the existing mock server.

| Check | Pins |
|---|---|
| a **fresh** chapter emits `chapter_start`, `stage begin/end` × 8, `chapter_end` in order | stage coverage |
| a **resumed** chapter emits only the stages it enters, and no TRANSLATE `stage` | M1 |
| `stage` `phase` is `begin` then `end` for each stage, with `elapsed_s` ≥ 0 | transition shape |
| a FAITH rejection emits `gate` with **all** reasons, then `attempt_failed`, then a fresh `attempt` | the gap this plan closes |
| **a chapter that raises mid-run still emits `chapter_end` with `outcome: "crashed"` and a `close` index line (payload best-effort)** | F13, R3 |
| a skipped chapter (`pipeline.py:903-905`) emits no `chapter_start`/`chapter_end` and leaves no index lines | R3 |
| **the chapter after a raised chapter logs nothing into the failed chapter's dir** | F13 (no scope to leak — the ContextVar hazard is gone by construction) |
| a `needs-review` outcome emits `chapter_end` with `outcome` and the run marks the chapter | failure path |
| consensus degradation emits `degraded` with `where: consensus` | degradation visibility |
| a glossary-cleanup failure emits `degraded` | existing `[warn]` path stays visible |
| `chapter_end` tokens are non-zero for a chapter that made calls | accounting |

### 4.4 `tests/test_logs_cli.py` (new)

| Check | Pins |
|---|---|
| `logs 7` prints chapter 7's timeline and exits 0 | happy path |
| `logs 9` with no logs exits 1 with the normative `[FAIL] no logs for spec: …` line | exit 1, R13 |
| `logs 99-100` exits 2 (asserted on the **exit code**, not the exception type) | F16 |
| `logs 1-5` resolves through `parse_range` | spec parity |
| `logs` bare prints the newest run | default |
| `--last 3` with a SPEC prints three runs newest-first from that chapter's index; `--last 3` without prints three root runs; `--last 0` exits 2 | L2, R13 |
| `--list` prints available run ids and exits 0 | L4 |
| `--run <exact id>` filters across all three buckets (one id selects the whole run); `--run <ambiguous prefix>` exits 2 listing deduped candidates | L4, R13 |
| `--json` emits one parseable JSON object per line and **zero** non-JSON lines | machine output, R13 |
| `--no-io` omits prompt and response from printed output | verbosity flag |
| `--report` writes `report.md` and exits 0 | report trigger |
| `--report --io` includes `<details>` blocks; `--report` alone does not (fixtures run with `log_prompt_bodies: true`) | F14 single-writer split, R13 |

### 4.5 `tests/test_log_report.py` (new)

| Check | Pins |
|---|---|
| `report.md` contains every required section heading | structure |
| every gate reason from the run appears verbatim | no truncation |
| all run ids found by the directory scan appear in the header | coverage, R14 |
| **a model reply containing `</details>` does not break the report's structure** | F14 escaping |
| a report raised inside `logreport.py` leaves the previous `report.md` intact and the pipeline running | F14 error policy |
| `chapter_end`'s auto-written report contains **no** `<details>` blocks | F14 bounded work |
| with `log_orchestration: false`, the auto-written report is header-only and the index `close` line still lands | R4, R14 |
| the call table's token totals match the JSONL | no invented numbers |
| `<details>` count equals the number of calls that carried bodies | completeness |
| a chapter whose runs were all pruned regenerates as a valid report marking the entries stale | M5 |

### 4.6 Existing suites to update

| Suite | Change |
|---|---|
| `tests/test_logger.py` | **Moves here from §4.1 — it needs edits, not just additions (F10, R6).** `case_3_prune` (`:133-161`) builds five `llm-*.jsonl` files and asserts `_prune(base, 2)` leaves `["llm-00003.jsonl", "llm-00004.jsonl"]`; with a `run-*.jsonl` glob `left == []` and that case fails — rewrite it for the new glob plus the `index.jsonl` exclusion. `case_4_two_projects` (`:192-216`) reads `logger._run_path` and asserts its parent is `logs/` — the two `_run_path` assertions (`:202-203`, `:212-213`) stay valid **only because §3.4 pins `_run_path` to the tier-1 path**, a deliberate load-bearing choice; but the case's helpers are hardwired to the old naming and fail anyway: `events_in()` globs `llm-*.jsonl` (`:74`), `run_name_ok()` regexes it (`:83`), and three direct globs (`:185-186`, `:193`, `:197`) return `[]` — rewrite them for `run-*.jsonl` and the new buckets. |
| `tests/test_consensus.py` | `read_log_events` (`:121-128`) globs `logs/llm-*.jsonl` at `:124`; fan-out lines now live outside the root — chapter dirs for chapter-carrying calls, `logs/project/` for this suite's chapter-less calls (R6) — so the helper must scan recursively. `reset_globals()` (`:131-135`) resets `_run_path` and must still reset both tiers. |
| `tests/test_cleanup_flow.py` | Resets `logger._run_path` at **`:216`, `:319`, `:405`, `:447`** — four sites, all of which must keep working under the new cache. (v1/v2 cited `:215`/`:318`, which are the preceding comment lines.) The trace reader also globs `llm-*.jsonl` at **`:249`** (docstring `:24`), and checks 3a/3b/3c assert on it — move it to the tier-1 root `run-*.jsonl` glob (R6). |
| `tests/test_migrate.py` | `v011` adds the three keys, rewrites `log_llm_keep_runs` only when it equals 5, is idempotent on re-run, and becomes the chain head. The v011 case's fixture must write the **literal `5`** into its `config.json` (the suite's pattern for specific values, e.g. `test_logger.py:122-126`): folding `config.DEFAULTS` post-v011 yields 10 and never exercises the rewrite branch. The suite's stale head-version references — the module docstring (`:24-25`, "chain … v008 / current_version() == 8"), the comment at `:191` ("real chain head (8 since v008)"), and the `case_4_real_chain` docstring (`:540-541`, "exactly [v001…v008], head version 8") — must be updated (R16; the assertions themselves are computed dynamically and survive v011). |
| `tests/test_command_defaults.py` | `logs` subcommand defaults. |

### 4.7 Doc-mirror checks

`AGENTS.md` mandates that changed defaults, keys, **exit codes**, flags, console
output, and schemas reach the matching docs, and that README restates the
maintenance workflows. Add checks to an existing suite:

| Check | Pins |
|---|---|
| `references/file-formats.md` documents all five log keys with their defaults | config table at `:219` |
| the `logs/` tree at `file-formats.md:38` shows `chapters/`, `project/`, `index.jsonl`, `report.md` | layout mirror |
| **`file-formats.md` § Exit codes (`:1439`) lists `translate logs`** | F12 — new exit-1 condition |
| **`README.md` documents the `translate logs` workflow** | F12 — AGENTS.md:19-21 |
| `README.md:236-243` no longer names `logs/llm-*.jsonl` as where to find `glossary_cleanup` | H2 |
| `README.md` no longer documents `logs/llm-*.jsonl` as current (`:756-781`) | stale-glob check |
| `references/maintenance.md` documents `translate logs` with its flags, defaults, and exit codes | maintenance mirror |
| `SKILL.md` carries the `logs` trigger line in a named slot (a Debugging subsection; commands are lifecycle `###` headings, there is no command table) | agent trigger, R15 |
| `references/file-formats.md` § Migrations lists `v011` and its untouched-default rule | migration contract |
| **`references/file-formats.md` gains a `logs/` schema section**: the two run-file tiers, both `index.jsonl` line shapes, stale marking, and `report.md`'s section structure | R15 — AGENTS.md mandates every file schema |
| **`README.md:751-752`'s file-schema enumeration covers the new files** (or is generalized) | R15 |
| **`references/maintenance.md`'s intro (`:1-5`) enumerates `logs` among the commands it covers** | R15 |

## 5. Phases

| Phase | Work | Gate |
|---|---|---|
| 1 | `lib/logger.py` rewrite: three buckets, per-project `run_id`, dual retention, body toggle, centralized gating, reset mechanism incl. counters, `index.jsonl` prune exclusion; `lib/config.py` keys; `scripts/migrations/v011.py` (hand-written) | §4.1 green, `uv run tests/run_all.py` green |
| 2 | `chapter=` threading (the existing `stem` at `pipeline.py:901`) through `consensus.chat` / `pipeline._chat` and all 11 sites (closure-rebind protocol for `tn_recheck`/`story`); `_trace_hook` emits ungated; `pipeline.py` stage / gate / degraded / chapter_start / chapter_end / run_start / run_end incl. the crash path and the skip decision | §4.2, §4.3 green |
| 3 | Tier-2 enrichment: `result`/`chunk`/`feedback`, both `index.jsonl` writers with stale marking | §4.2 green |
| 4 | `lib/logreport.py`; `cmd_logs` + parser block in `scripts/translate.py` | §4.4, §4.5 green |
| 5 | Docs + `uv run tests/run_all.py` | §4.7 green, full suite green |

Phases 1-3 are the substance; 4 is convenience; 5 is the `AGENTS.md` obligation.

## 6. Risks

| Risk | Mitigation |
|---|---|
| Log volume. Full prompts and responses across a consensus fan-out with retries reach tens of MB per chapter | Retention caps on all three buckets; `log_prompt_bodies`; tier 1 never carries bodies; `chapter_end` writes a metadata-only report |
| A missed `chapter=` kwarg silently files a chapter's IO in the project bucket | Explicit threading makes the default visible: `chapter=None` is a deliberate, greppable choice at the three project-scoped sites. §4.2's `tn` check covers the case that bit v2 |
| Reset contract breakage. Six sites in three suites poke `_run_path` | §3.4 mechanism; `_run_path` retains its tier-1 meaning so `case_4_two_projects` passes unmodified |
| `_prune` overreach — a `logs/*.jsonl` glob would delete chapter files, `index.jsonl`, or `epub-build.log` | Prune by exact name shape per bucket; `index.jsonl` excluded; §4.1 checks |
| `v011` hiding the retention change | §7 hand-written step in the `v009` shape; `test_migrate.py` pins the report line |
| Report generation on the critical path | §3.10: `chapter_end` writes metadata only, bodies only on demand, atomic replace, HTML-escaped, never raises |
| Event-name typos silently defaulting to tier 1 | Explicit table, §4.2 table-driven check |
| Config read per event | §3.8 resolve-once-per-run |
| Crash-path `chapter_end` under-reports (aggregation cut short) | §3.4 counters + §3.6 best-effort contract; the index `close` line is the reliable crash signal |

## 7. Migration `v011` (C3, F11)

**`v011`, not `v010`.** The 4-digit canonical rename shipped as `v010.py`
(2026-10-07), so this step takes the next number. `chain()` discovers it with no
registry edit.

**Hand-written, not a `standard_step()` delegation.** Two jobs, two treatments:

1. **`log_llm_keep_runs` 5 → 10** follows the `v009` pattern: rewrite the
   **raw** file when the value exactly equals the old default `5`. This is not
   optional polish — `materialize_config` folds *all* `DEFAULTS` into
   `config.json` on every `init` and `migrate` (`common.py:27-46`), so with
   `config.py:50` currently `"log_llm_keep_runs": 5`, essentially every existing
   project already carries an explicit `5`, and a bare `standard_step` would
   preserve it. That would deliver the owner's locked decision to new projects
   only. `v009.py:16-21` rules on this exact case: "Values are rewritten only
   when they EXACTLY equal the old default, the same 'untouched default' rule
   the rest of this chain uses." `v011`'s docstring states the same ambiguity
   and the same recoverability for its own number.
2. **The three new keys** (`log_orchestration`, `log_prompt_bodies`,
   `log_chapter_keep_runs`) are added by folding `config.DEFAULTS`, reported
   explicitly as new keys.

Then `common.sync_templates(...)` — no template changes, so it stays silent on
an up-to-date project. Idempotent: the second call matches the new default, the
rewrite matches nothing, and `migrate()` reports `[]`.

**Permanent-baking consequence, stated for the record (M2):** because
`materialize_config` writes the entire merged form, after `v011` every project
carries `"log_llm_keep_runs": 10` as an explicit literal, which will
thereafter no longer track `DEFAULTS`. `v009` accepts this trade-off and
documents it; the plan accepts it too, because leaving existing projects on the
old default contradicts the owner's retention decision.

## 8. Acceptance criteria

1. `uv run tests/run_all.py` passes, with no suite reporting zero checks.
2. A clean three-chapter `translate 1-3` run produces exactly three
   `logs/chapters/Chapter_NNNN/` directories, one tier-1 file, one project-bucket
   `index.jsonl`, and no new `llm-*.jsonl` files.
3. Parsing every line of `logs/run-*.jsonl` as JSON, **no line has a `prompt`
   or `response` key**, at every verbosity. (L1 — replaces v1's grep, which
   false-positives on `glossary_review`'s arbitrary reviewer text and assumed a
   POSIX `grep` on a Windows runner. F18.)
4. Every `run_id` in the tier-1 file equals every `run_id` in all three
   chapters' tier-2 files (F4).
5. With `log_llm: false`, tier 1 still contains one `llm_call` per model call,
   and each chapter's tier-2 file exists with `chunk`/`result`/`feedback` and no
   `llm_request` (C1, F1).
6. After `translate tn 1-3`, each chapter's annotator IO is in that chapter's
   tier-2 directory and nowhere else (F2).
7. After a `retry` of chapter N with a missing N-1 recap, the backfill call is
   in N-1's tier-2 directory and not in N's (F3).
8. A chapter that raises mid-run still yields `chapter_end` with
   `outcome: "crashed"` and an index `close` line (F13).
9. Every chapter's `report.md` reproduces every gate reason verbatim, contains
   no `<details>` when written by `chapter_end`, and survives a reply containing
   `</details>` when written with `--report --io`.
10. `translate logs 7` exits 0; `translate logs 99-100` exits 2; a spec with no
    logs exits 1; `--run <ambiguous prefix>` exits 2 listing candidates.
11. `migrate` bumps `log_llm_keep_runs` 5 → 10 **only** when it equals 5, adds
    the three new keys, reports the bump explicitly, and is idempotent.
12. All doc mirrors in §4.7 are true against the shipped code.

## 9. Doc mirrors to update in the same change (AGENTS.md)

| File | Change |
|---|---|
| `references/file-formats.md` | `logs/` tree (`:38`); all five log keys in the config table (`:219`); the `logs` row in § Exit codes (`:1439`); `v011` in § Migrations; **new `logs/` schema section** — run-file tiers, both `index.jsonl` line shapes, stale marking, `report.md` structure (R15) |
| `references/maintenance.md` | new `translate logs` section: flags, defaults, exit codes; **the intro's covered-command list (`:1-5`) gains `logs`** (R15) |
| `SKILL.md` | a trigger line for the `logs` command in a named slot (Debugging subsection; commands are lifecycle `###` headings, no command table) (R15) |
| `README.md` | the `translate logs` workflow (`:236-243` `glossary_cleanup` pointer, `:756-781` logging section); **the file-schema enumeration (`:751-752`) covers the new files** (R15) |