# Plan — project-wide HTML dashboard for `logs/`

**Date:** 2026-10-08
**Status:** proposal, not yet implemented.
**Author scope:** `D:\Repos\vibed\translate-skill` @ working tree.

## 1. Goal

`report.md` is the human-readable face of **one chapter's** trace. There is no
equivalent for the project as a whole: the maintainer who just ran `translate
1-40` has eleven chapter buckets, a dozen run files and an epub log, and the only
way to read them is `translate logs --list` plus one `report.md` at a time.

This adds **`logs/report.html`** — a single self-contained page that assembles
every log the project retains and presents it graphically: where the wall-clock
went, where the tokens went, which chapters failed, and which model served
which job.

### 1.1 Owner decisions (locked 2026-10-08)

| Fork | Decision |
|---|---|
| Output | One page, `logs/report.html`, mirroring `report.md`'s naming |
| Scope | Whole project always — a `SPEC` does **not** narrow it (the ask is explicitly whole-project) |
| Fidelity | Self-contained: **zero** network requests, no CDN, no webfonts |
| Bodies | Metadata by default; prompt/response bodies opt-in via `--io` and capped |
| Wiring | Auto-refresh at `_run_end` **and** an explicit `translate logs --html` |
| HTML risk | Never raises, never partially overwrites a good dashboard |

### 1.2 Binding constraints

1. **Never-raise.** `lib/logdashboard.py` inherits `logreport.py`'s contract:
   any failure prints one `[warn]` and leaves the previous `report.html` in
   place. `project.atomic_write_text` makes the "never partially overwrites"
   half true for free.
2. **Escape everything.** Model text is arbitrary and can contain `</details>`,
   `</script>`, fenced blocks and raw HTML. `logreport.py:17-19` already records
   why escaping is load-bearing; the dashboard inherits that rule for every
   interpolated value.
3. **No invented numbers.** A missing/malformed `usage` contributes zero and
   renders as `—`, never a guess. Same discipline as `logreport._usage_tokens`.
4. **Quality outranks quota cost** (`AGENTS.md`) — no routing, fan-out or
   `max_tokens` change here.
5. **Doc mirrors ship in the same change** (`AGENTS.md`) — §6.
6. `logs/` is gitignored (`file-formats.md:99`), so the new file needs no
   gitignore work and is never committed.

## 2. Evidence — what the logs actually contain

Measured against `example_project/` (11 chapter buckets, 2026-10-06 → 10-08).

### 2.1 Sources, by cost

| Source | Size in example | Pruned? | Carries |
|---|---|---|---|
| `logs/run-*.jsonl` (tier 1) | **absent** (see §2.2) | yes, `log_llm_keep_runs` | `run_start`/`run_end`, `chapter_start`/`end`, `stage` begin/end + `elapsed_s`, `gate` + reasons, `degraded`, `llm_call` (job, model, candidate, usage, elapsed_s, finish_reason, error) |
| `logs/chapters/<stem>/index.jsonl` | **7,662 B for all 12 index files, measured** | **never** | per chapter-run `open` (ts, command, file, number) / `close` (outcome, attempts, stages, calls, `tokens{job}`, `elapsed_s`) |
| `logs/project/index.jsonl` | (of the 7,662 B) | **never** | per run `open` (ts, command) / `close` (outcome) |
| `logs/epub-build.log` | 22 KB | no | 53 `=== epub build after <file> \| ts ===` blocks over **48 distinct chapter names**, with `[ok]`/`[warn]` markers |
| `logs/chapters/<stem>/run-*.jsonl` (tier 2) | **4,005,028 B over 15 files, measured** (up to 239 KB each) | yes, `log_chapter_keep_runs` | `llm_request`/`llm_response` **bodies**, `result`, `chunk`, `feedback` |

### 2.2 The finding that shapes the design

> **RETRACTED after audit, and replaced.** The original §2.2 claimed tier 1 is
> usually *absent* because retention prunes it. That was wrong, and the
> independent audit refuted it three ways against the sample data:
>
> 1. Only **9 distinct run ids ever existed** across all twelve `index.jsonl`
>    files, and `log_llm_keep_runs` is 10. `_prune` deletes `runs[keep:]`, so
>    retention **deleted nothing**.
> 2. The same `_prune` demonstrably *retains* its limit on this very tree:
>    `log_chapter_keep_runs` is 3 and the chapter buckets hold exactly 3 files.
> 3. Tier-1 files **existed and were deleted afterwards**: `logs/` has
>    `CreationTime` 05:13 today while every file inside keeps an mtime from
>    10-06 → 10-08, and `CHAPTER_0008/report.md` carries a full stage
>    timeline — which `logreport.tier1_rows` can only produce by reading
>    `logs/run-*.jsonl`. The sample tree was assembled selectively.
>
> The fixture was the artifact. The lesson was recorded where it belongs, in
> the test: `case_1_primary_path` builds a tree **with** tier 1 and asserts the
> rich rendering, so the signature element can no longer be specified against
> its own degraded branch.

**The corrected finding.** Both tiers are needed, and they cover different
spans:

- **`index.jsonl` is the complete spine.** Append-only, never pruned, so it
  carries the *entire* history of every chapter — outcome, attempts, stage
  count, calls, per-job tokens and wall-clock for every run the project ever
  made.
- **Tier 1 is a windowed enrichment**, covering only the newest
  `log_llm_keep_runs` runs. It is the *only* source of stage spans, gate
  verdicts, the per-call table, the per-model split, and **chapter-less spend**.

So the dashboard draws from both, names which one supplied each total, and
treats tier-1's absence as a stated degradation rather than a normal case.

This also bounds the cost: the spine is **7.7 KB of index files, measured**;
tier-2 is megabytes and is read only under `--html-io`.

### 2.3 Two measured hazards in the enrichment sources

**(a) `epub-build.log` chapter names are mixed case, and the file is written by
a background child.** Its block header carries the chapter *filename*:
`autobuild.py:138` writes `=== epub build after {reason} | {stamp} ===`, where
`reason` is whatever the pipeline passed. Measured across the example: the first
block says `Chapter_0001.md` and the last says `CHAPTER_0012.md` — pre-`v010`
runs predate the rename, so **48 distinct names spell 53 blocks across two
casings**. Every bucket on disk is `CHAPTER_NNNN`. Therefore the join must be
**case-insensitive, on the stem**, and must tolerate a chapter built more than
once (keep the latest block per chapter, count rebuilds).

Worse, the file is **appended by a concurrently running subprocess**
(`autobuild.py:135-144` opens the log, then `Popen`s `build-epub` with that
handle as stdout). A dashboard rendered at `_run_end` can read a half-written
final block, so an **unterminated trailing line is skipped** rather than parsed.

**(b) `_run_end` does not run on the interrupt path.** `main()` maps
`KeyboardInterrupt` to a hard `os._exit` when a fan-out is live
(`translate.py:2275-2283`), so no `run_end` — and therefore no auto-refresh —
happens for the interrupted run. That is precisely when a maintainer wants the
dashboard. Consequence for the design: **the auto-refresh is a convenience for
the happy path, and `translate logs --html` is the investigative path.** This
is why §4 keeps both.

### 2.4 Chapter identity

`logger.bucket_dir` keys a chapter on `Path(file).stem` (`logger.py:191`).
`project.CHAPTER_RE` is **`^CHAPTER_([0-9]{4})\.md$`** — uppercase and
deliberately **case-sensitive** (`project.py:32`, with the reasoning spelled out
at `project.py:22-24`: case-insensitivity would let `Chapter_0042.md` and
`CHAPTER_0042.md` both parse to 42).

So the numeric sort key is the four-digit group. But a stem that does **not**
match `CHAPTER_RE` is still reachable on disk: `migrations/v010.py` renamed
project files to the new form, and a pre-migration project's log buckets keep
their old names. Those rows are **kept and sorted last** (numeric first, then
stem, then original order — a total order, so nothing raises and nothing
disappears), and the page says how many such legacy buckets it found rather than
hiding them.

A bucket can also exist for a chapter no longer in `chapters.json` (source
deleted, chapter retired). Those rows are kept — the logs are the record of what
happened — and marked `not in manifest`.

### 2.5 Tier 2 carries the call record — a gap found late

Raised by the maintainer pointing at
`logs/chapters/CHAPTER_0011/run-20261008-034557-translate-3353909.jsonl`:
*"didn't I include run logs in the example project?"* Yes — and the first
implementation read calls **only** from tier 1's `llm_call` summaries, so a
project whose orchestration tier had aged out showed an empty model roster and
no per-call table while that 320 KB file sat there holding all eleven calls.

An `llm_response` line carries everything `llm_call` does — `job`, `model`,
`candidate`/`candidates`, `consensus_for`, `usage`, `elapsed_s`,
`finish_reason`, `error` — plus `call_id` and `url`, and the tier-2 bucket is
chapter-scoped by construction so nothing needs filtering. The per-call table,
the per-model split and every call-derived total now come from **tier 2**,
newest retained file per chapter; tier 1 answers only for a chapter with no
tier-2 call record, i.e. one written while `log_llm` was off.

The cost objection that had been holding this back was **measured wrong**:
a full `json.loads` pass over every retained tier-2 file in the project (4 MB,
15 files, 333 lines) costs **25 ms**. The files are large but few. Newest-file
only keeps the render O(chapters). End-to-end collect on the sample project is
now **41 ms** (was 22 ms) and the model roster is populated instead of empty.

**Net effect on the sample project:** 110 → 116 calls, 2,133,763 → 2,204,837
tokens, and a real model roster (glm-5.3 966,859 / MiniMax-M3.1-Flash-Preview
712,620 / glm-5.3-flash 525,358).

## 3. Desired behavior

### 3.1 Data assembly (`collect()`)

Pure read, no writes, never raises. Returns a plain dict:

```
project      : {title, source_lang, target_lang, chapters_in_manifest, generated_at}
runs         : [{run_id, command, started, closed, outcome}]
chapters     : [{stem, number, runs:[{run_id, command, started, closed, outcome,
                                     attempts, stages, calls, tokens{}, elapsed_s}],
                 stage_spans:[{stage, attempt, elapsed_s, began, ended}],
                 gates:[{stage, verdict, reasons[]}],
                 calls:[{run_id, job, model, candidate, candidates, consensus_for,
                         prompt_tok, completion_tok, elapsed_s, finish_reason, error}],
                 epub:[{ts, ok, note}]}]
totals       : {runs, chapters, calls, tokens_by_job{}, tokens_total, elapsed_s}
health       : {outcomes:{outcome:count}, unclosed:[run_id...], degraded:n}
gaps         : [strings]     # what is missing and why (see §3.5)
```

### 3.2 The ledger (signature element)

One row per chapter, and **the row is the chart**: a horizontal stacked bar of
that chapter's stage durations, tinted by outcome. Scanning the column of bars
answers "which chapters were slow, and which ones are red" without reading a
number. Bar segments are `stage` spans from tier 1; with no tier 1 the bar is one
neutral segment of `elapsed_s` and the row is labelled *stage detail unavailable*.

Rationale: the subject is a **ledger of a batch**, and a ledger's native form is
a ruled column of proportional entries. The design spends its one bold move here
and keeps the rest of the page quiet.

### 3.3 Sections

1. **Masthead** — project title, `zh → en`, generated-at, and the run ledger
   totals set as a single tabular line (not three cards).
2. **Ledger** — §3.2, the whole-project time-and-health view.
3. **Where the tokens went** — per-job horizontal bars, split by model.
4. **Model roster** — job × model matrix with call counts and error marks.
5. **Health** — outcome distribution, degraded count, and the unclosed-run
   crash signal called out first because it is the one thing that must not be
   missed.
6. **EPUB builds** — parsed from `epub-build.log`, joined case-insensitively on
   the stem, latest block per chapter, rebuild count shown (§2.3a).
7. **Model exchanges** — only with `--io`, capped (§3.6).

### 3.4 Rendering rules

- **Fully static render.** Python emits final HTML. There is **no embedded JSON**
  and no `<script>` containing model text, which removes the `</script>` escape
  class entirely. Interactivity (tooltip, outcome filter, sort) is vanilla JS
  reading `data-*` attributes only.
- Every interpolated value goes through `html.escape(..., quote=True)`.
- Self-contained: system font stacks only, inline `<style>`, no external URL.
  A CDN webfont would both break offline use and leak the fact that the page was
  opened to a third party; the type personality is carried by scale, weight,
  tracking and case instead.

### 3.5 Honest gaps

Every absence is stated in the page, never rendered as a zero:

- no tier 1 → *orchestration tier unavailable; stage detail and gate verdicts omitted*
- `epub-build.log` absent → the EPUB section is omitted, not empty
- a chapter with `open` but no `close` → listed under **unclosed**, and flagged
  in the ledger row as `did not close`
- `log_orchestration: false` → exchanges show `prompt_chars`/`response_chars`
- a bucket whose stem is not `CHAPTER_RE`-shaped, or whose chapter is absent
  from `chapters.json` → kept, marked, counted in the header (§2.4)
- an `epub-build.log` whose final block is half-written by a live build →
  that block is skipped, and the page says N trailing lines were unreadable

### 3.6 `--io` bodies, capped

Opt-in only. Cap at **200 KB** of body text total; when the cap is hit the page
says *N further exchanges omitted (size cap)* rather than truncating silently.
This bounds a dashboard that would otherwise embed ~8 MB of model text.

## 4. CLI and wiring

| Surface | Behavior |
|---|---|
| `translate logs --html` | Writes `logs/report.html` (metadata-only), prints `[ok] html: <path>`. **Handled before run selection**, so it writes a page even when this invocation has no events — the early `return 1` paths below read only the orchestration tier. |
| `translate logs --html --html-io` | Adds the exchanges section, capped at §3.6. Deliberately a *separate* flag: `--io`'s default follows `log_prompt_bodies` (true), so reusing it would embed model text by surprise. |
| `translate logs <spec> --html` | Same page — the dashboard is whole-project; a `SPEC` does not narrow it. |
| `_run_end` | Refreshes the dashboard after every `translate`/`retry`/`tn`/`review`/`profile` run. A convenience for the happy path, **not** a freshness guarantee: `main()` hard-exits on an interrupt (§2.3b), which is why `--html` exists. |

**No new config key.** The write is cheap (7.7 KB of indexes in, ~30 KB of HTML
out, ~35 ms measured), never raises, and lands in a gitignored directory;
adding `log_html` would be a schema change and therefore force a `v015`
migration under `AGENTS.md` for a switch nobody asked for.

**Bounded retry (audit M5).** This path deliberately does **not** use
`project.atomic_write_text`, whose `os.replace` retry ladder backs off
0.1s…3.2s — ~6.3 s. The usual cause of a held destination here is someone
*viewing* `report.html` in a browser, and stalling a finished translate for six
seconds to then print `[warn]` is the wrong trade. `logdashboard._write_page`
gets three short attempts (~0.3 s) and is still atomic.

**`--json` purity** (`translate.py:2180-2184`) is preserved: with `--json`, the
`[ok] html:` marker is suppressed exactly like `[ok] report:` is today.

## 5. Files

| File | Change |
|---|---|
| `scripts/lib/logdashboard.py` | **new** — `collect()`, `render()`, `write_dashboard()`, `refresh()` |
| `scripts/translate.py` | `--html` / `--html-io` on `logs`; `write_dashboard` from `cmd_logs` (before the early returns) and `refresh` from `_run_end` |
| `tests/test_log_dashboard.py` | **new** — 71 checks, house style |
| `references/file-formats.md` | `logs/report.html` in the project tree; the § `index.jsonl` / `report.md` section extended with the tier-1 windowing, the three honest gaps, the attempt-summing divergence, and the no-network/bodies contract |
| `references/maintenance.md` | the `--html` / `--html-io` flags, the `[ok] html:` marker, and the revised exit-code line |
| `README.md` | the *Reading the logs* section |
| `SKILL.md` | the trigger line only |

**No migration** — no `config.json` key and no template change (`AGENTS.md`).

## 6. Validation

`uv run tests/run_all.py` must pass. `tests/test_log_dashboard.py`, 71 checks:

1. **primary path** (11 checks) — a tree WITH tier 1 yields stage spans, gate
   verdicts, the per-call table, candidate ratios and the per-model roster, and
   renders no "unavailable" banner. This is the case the audit found untested.
2. **escaping** — a response carrying `</script><img onerror=…>`, a fenced
   block and a closing `</details>` appears escaped; the raw string appears
   nowhere; the page's single `<script>` holds no log-derived value.
3. **no network** — no `src="http`, `href="http`, `url(http`, `@import`,
   `<link>` or `<iframe>`; exactly one inline `<script>`.
4. **no invented numbers** — `usage: null` contributes a call and zero tokens
   without becoming "unknown"; unknown duration/count render `—`; real zero
   renders `0`; malformed usage contributes zeros.
5. **tier-1-absent** — an explicitly synthetic index-only tree renders a
   complete page naming the missing enrichment, and declares chapter-less spend
   unknown rather than zero. (The real sample project is *not* this case; §2.2.)
6. **attempt summing** — a stage retried across two attempts contributes both
   segments and they sum, proving the deliberate divergence from `report.md`.
7. **ordering** — `CHAPTER_0100` sorts after `CHAPTER_0099`; a legacy
   `Chapter_0099` bucket sorts last, is kept, and is reported.
8. **unclosed / recovered** — an open with no close surfaces once (not once per
   bucket); a chapter whose last run crashed then translated reads green; a
   chapter whose *last* run died reads `unclosed`.
9. **`--html-io` cap** — bodies absent by default, present with the flag, an
   oversized single body omitted whole, and the omission stated.
10. **epub join** — a differently-cased bucket still joins, the latest block
    wins, and the maintainer's absolute home path is redacted.
11. **torn tail** — a half-written final block is skipped and counted.
12. **never-raises** — a locked bucket still yields a page; an unwritable
    destination returns `None` and raises nothing; `refresh` never raises.
13. **CLI** — `--html` writes and exits 0 where bare `logs` would exit 1,
    `--json` stdout stays parseable, and plain `logs` is untouched.
14. **bodies off by default** — with `log_prompt_bodies: true`, `--html` alone
    embeds nothing and `--html-io` does.

## 7. Open questions for the owner

None blocking. Two defaults were chosen and can be flipped cheaply if unwanted:
the 200 KB `--io` cap (§3.6) and the auto-refresh at `_run_end` (§4).