Agent-facing workflows for glossary, review-report, and translator's-note
maintenance: semantics, flags, defaults, console markers, and exit
behavior for `review glossary|notes|fix`, `tn`, `logs`, `glossary
set|merge|retire|replace|search|count`, and `util replace`. File formats
and config schemas live in `references/file-formats.md`.

## Reading the trace logs (`logs`)

Read-only. Nothing here mutates project state except `--report` and `--html`,
which regenerate a derived report.

```
uv run "$SCRIPT" logs                       # newest run's orchestration timeline (tier 1)
uv run "$SCRIPT" logs 7                     # one chapter's model IO (tier 2)
uv run "$SCRIPT" logs 1-5 --last 3          # a range, newest 3 runs
uv run "$SCRIPT" logs --list                # run ids, command, start time, chapter count
uv run "$SCRIPT" logs --run 20261007-142233-translate-4711
uv run "$SCRIPT" logs --run 20261007         # unique prefix also works
uv run "$SCRIPT" logs 7 --json --no-io      # JSON objects on stdout, nothing else
uv run "$SCRIPT" logs 7 --report --io       # regenerate report.md with model bodies
uv run "$SCRIPT" logs --html                # whole-project dashboard -> logs/report.html
```

**Two views.** With no SPEC you get the **orchestration timeline** (tier 1:
run/chapter lifecycle, stage transitions, gate verdicts, degradations, one
`llm_call` summary per model call). With a SPEC you get **that chapter's model
IO** (tier 2: `llm_request` / `llm_response` / `chunk` / `result` /
`feedback`). A SPEC resolves exactly as `translate` / `retry` / `mark` do —
`7`, `1-5`, `CHAPTER_0007.md`.

**`--run`** takes an exact run id or a **unique prefix** (a run id embeds a
`<pid>` the operator cannot know). An unknown or ambiguous value is exit **2**
and lists the ids that do exist. One run id spans its tier-1 file, every
chapter's tier-2 file and the project bucket, so selecting a run selects the
whole run.

**`--io` / `--no-io`** control prompt/response bodies; the default follows
`log_prompt_bodies` in the project's `config.json`.

**`--json`** emits one JSON object per event and **nothing else** on stdout —
no summary line, no chapter headers — so the stream pipes straight into `jq`.
House `[ok]` / `[warn]` / `[FAIL]` markers still go to stderr on error paths.

**`--last N`** selects the newest N runs (default 1). With a SPEC the runs
come from that chapter's own `index.jsonl`, newest first.

**`--report`** regenerates `logs/chapters/<stem>/report.md` for each matched
chapter from its retained runs. `--io` additionally wraps every call that
actually carried a body in a `<details>` block.

**`--html`** writes `logs/report.html`, the project-wide dashboard: every
retained log in one self-contained page — a per-chapter ledger of stage
durations tinted by outcome, token spend per job and per model, gate verdicts,
the unclosed-run crash signal, the epub build history, and a **call ledger**
listing every model call of every chapter's latest run. The same write also
emits **`logs/report.json`**, the `collect()` output behind the page: the
diffable data contract, so two runs can be compared without opening either
page. Both are derived and gitignored; delete either and the next dashboard
run rebuilds it. The page carries its stylesheet and script inline (from
`assets/dashboard/report.css` and `assets/dashboard/report.js` in the skill), so
a single report can be archived or emailed on its own — it makes no external
reference and no network request. It is handled
*before* run selection, so it writes a page even when this invocation has no
events to print, and it always covers the **whole project** — a SPEC does not
narrow it. `_run_end` refreshes the same file after every `translate`/`retry`/
`tn`/`review`/`profile` run, so it is normally already current; run it by hand
after an interrupt, which hard-exits without reaching `_run_end`. Console:
`[ok] html: <path>`.

**Bodies are always embedded, uncapped.** Clicking a row in the ledger
expands it — anywhere in the row, not just the call id, and Enter/Space work
too: the prompt, the response parsed into its schema's view (a
source-vs-translation side-by-side for `lines`, tables for `terms`/`notes`/
`decisions`, a badge for `verdict`), the raw body, and the request params.
There is no `--html-io` flag and no byte cap — the old 200 KB cap dropped 84 of
131 bodies on a real project, oldest run first, and separating bodies behind a
flag only produced "why is it still saying no bodies" reports on a project that
plainly had them. Embedding costs ~36 ms against a 981 s chapter. The page
makes no network requests.

**Exit codes:** 0 printed something (and, with `--html`, wrote the page); 1
nothing found (no logs at all, or no logs for that spec) with a `[FAIL]` line,
or the dashboard could not be written; 2 an out-of-range spec or an unusable
`--run` value.

A `logs/` directory written before v11 (`llm-*.jsonl`) is never listed,
matched or pruned.

## Glossary upkeep

Hand-fix bad entries any time (`glossary search` is the read-only lookup —
see Bulk review fixes); the balance check reads
`glossary.json` fresh for every chapter. Nothing in the pipeline audits
ENTRY quality — `uv run "$SCRIPT" review glossary --project .` does: a
deterministic heuristic tier (cross-entry duplicate/variant collisions;
a translation shared by other entries (info); translation still in the
source language or equal to the source; a missing category (info — legal
for minimal hand-added entries, other stages treat it as 'other');
unknown category; non-CJK text in a CJK entry's variants (info)) plus
the `glossary` provider judging source-translation alignment,
definitions, categories, cross-entry
conflicts, and mundane terms (entries that are not named entities,
named actions, or titles bound to a name — class nouns like 麦穗 "wheat
stalks" never belonged; seeded/catalogue and `category: "unit"`
entries exempt) in batches of
`review_batch_size`
(default 40; `--batch-size N` overrides per run)
through `templates/glossary_review.md`. With a multi-model `glossary`
array each batch fans out to every model in parallel and merges via the
consensus provider, announced by `[consensus] glossary: {n} model(s) -
merging results via the consensus provider`; candidate failures warn
and continue while any survivor remains (`[warn] consensus: glossary
candidate {i}/{n} ({model}) failed: {err} - continuing with the
remaining candidates`). Console sequence: the header
`[glossary] review: N entries (B model batch(es) of up to S)` prints
before the model calls, `[glossary] reviewing batch i/n` before each
batch, `[glossary] warn batch i/n review failed - <error>` after a
failed batch, then — after the per-finding lines — the post-run
summary `[glossary] review: N entries, W warn / I info findings` (W/I
are outstanding counts: findings `--fix` resolved drop out).
Report-only: one
`[glossary] warn|info` line per finding, never touching glossary.json
unless `--fix`; `--fix`
applies only model-suggested fixes (direct model-tier warn findings,
or a suggestion the merge borrowed onto a heuristic finding), to
translation/definition/category only, validated (valid category, no
source-language text), skipping conflicting suggestions and re-run no-ops —
a suggestion already equal to the live value (console:
`[glossary] fixed '<source>': <field> '<old>' -> '<new>'` per change).
Mundane findings carry no suggestion — `--fix` never retires them; the
report's `glossary retire` Command bullet does (see Bulk review fixes).
Exit 0 clean or info-only (or every warn fixed), 1 warns remain,
2 usage/setup error; empty glossary exits 0. Review flags are per
subject: `review glossary` accepts `--fix` and `--batch-size`,
`review notes` accepts `--chapters` and `--batch-size`, and `review
fix` accepts `--glossary` (the report path), `--dry-run`,
`--stale-ok`, `--exit-on-error`; anything else exits 2 with
`[FAIL] --<flag> does not apply to 'review <subject>'`. Cost ceil(N/review_batch_size)
model calls; model-tier failures fail safe per batch — heuristic findings
still report. Every run also writes indexed `<project>/review-report.md`
(filename from config `review_report_path`;
overwritten each run, clean runs too — a `review notes` run rewrites the
same file with the notes format, see Translator's-note re-evaluation;
console: `[glossary] report: <path>`)
— a YAML frontmatter block with the run's counts (entries reviewed, batch
errors, glossary digest, outstanding warn/info, machine-applicable vs
manual-review tallies, and the `[N]` indices of the manual-review
findings), then the
numbered OUTSTANDING findings in two sections: `## Machine-applicable`
(apply with `review fix`) — each finding with the full entry JSON + an
Action line + a `- Command:` bullet — then `## Needs manual review`
(decide yourself or hand to an agent). Tell an agent
"fix items 1,4,5 in review-report.md doing what was suggested", or hand
the manual-review items — the ones under `## Needs manual review` — to
an agent as a section, or run
`uv run "$SCRIPT" review fix --glossary review-report.md [--dry-run]
[--stale-ok] [--exit-on-error]` for the offline machine-actionable path (it applies
every `- Command:` bullet as a subprocess; exit codes, pre-validation,
and legacy-report handling are specified in Bulk review fixes below).
When a glossary translation changes (hand edit or `review
glossary --fix`), already-translated chapters still carry the old
rendering — fix them with `glossary replace`, not expensive retranslation
(`retry --chapters N`): `uv run "$SCRIPT" glossary replace --project .
--source 灵根 --translation "spiritual root" [--keep-alt] [--no-build]
[--dry-run]` finds the entry by source or variants (exit 2 when unknown),
rewrites the old rendering across chapters first and saves the new
`translation` to glossary.json last — a friendly no-op (exit 0) when the
translation already equals the new one. That ordering is the crash
recovery story: a run that dies mid-rewrite (e.g. a chapter file held
open by another process) prints `[warn] replace incomplete: k/N
chapters rewritten; glossary.json not updated - re-run the same command
to finish` and fails cleanly (exit 2) with glossary.json still saying
the old translation; re-running the same command finishes the job
(already-rewritten chapters match zero occurrences and are skipped, so
the re-run is idempotent). By default it also prunes the old rendering
from the entry's `alt_translations` (a stale alt would let the balance
check keep counting the old rendering as valid, masking drift), while
`--keep-alt` leaves alt_translations untouched for renderings that
should stay accepted variants; use `--no-build` to suppress the auto
epub build when running many replaces in a batch (e.g. from
`review fix`, which always passes it itself and runs exactly one final
epub build at the end).
`uv run "$SCRIPT" util replace --project . --source "spirit root"
--target "spiritual root"` is the raw-phrase variant for arbitrary term
fixes. Both are offline (no LLM calls) and match smartly, mirroring the
balance checker's phrase semantics minus the fuzzy tier: case-insensitive,
words may be joined by whitespace or hyphens, optional inflection
(es/s/ed/ing) on the last word — each match's capitalization is
preserved and the inflection re-appended (Spirit root → Spiritual root;
spirit roots → spiritual roots); CJK phrases replace as exact literal
substrings; footnote markers `[^N]` are never disturbed. Only the chapter
body is rewritten (everything after the frontmatter, a legacy baked-in
Translator's Notes section included; frontmatter stays byte-verbatim;
only files with matches are
rewritten, atomically, LF); chapters are enumerated from the manifest
(status `translated`), listed-but-missing files reported as warnings.
`--dry-run` reports the glossary diff and per-chapter occurrence counts,
writing nothing. Console: `[util] replace '...' -> '...'` /
`[glossary] '灵根' translation: 'old' -> 'new' (pruned alt: ...)`
headers, `[replace] Chapter_NNNN.md: N occurrence(s)` per changed
chapter, `[ok] replaced X occurrence(s) in Y chapter(s) (Z scanned)`,
`[warn] no occurrences found ...` (still exit 0). Exit codes: 0 success,
2 usage/setup (no manifest, unknown glossary term) or an interrupted
rewrite (glossary.json untouched — re-run the same command to finish).
After any chapter
actually changes (and not `--dry-run`), one synchronous epub build runs
when `auto_build_epub` is true (default) and novel_info.json exists
(console `[epub-auto] build ok (after util replace|glossary replace):
<path>`; failures warn only and never change the exit code); unreadable
config skips the build with a warning — the commands themselves don't
need config.json. Balance drift signals (advisory)
may auto-retire mundane glossary terms via `glossary_auto_cleanup`
(default true; retirement is applied only after the chapter's translation
is accepted; check console/trace `glossary_cleanup` events); nothing
blocks on balance anymore — enforcement is the FAITH reviewer's call.
Retired entries never come back via `seed` or GLOSSARY_EXPAND — remove a
source from glossary.json's `retired` list to allow re-adding. Every
mutating action in this section commits — `review: glossary audit` (suffixed
`review: glossary audit (N fix(es) applied)` when `--fix` landed fixes),
`glossary replace` / `util replace: '<src>' -> '<dst>'` — while
read-only `glossary search` / `glossary count` commit nothing.

## Translator's-note re-evaluation

Notes are stored per chapter in `notes/<stem>.json` sidecars (the chapter
markdown stays clean; `build-epub` renders them, falling back to parsing
legacy baked-in markers when a sidecar is absent). To re-decide which notes
a translated chapter range deserves — after swapping the annotator model,
tuning `max_notes_per_chapter` / `tn_gap_chapters` / `tn_keep_low_confidence`,
or updating the `tn_generate.md` template — run:

- `uv run "$SCRIPT" tn --project . --chapters SPEC [--dry-run] [--no-build]` —
  re-evaluate translator's notes on already-translated chapters: re-runs the
  annotator over the range (untranslated chapters are skipped with a
  warning) and regenerates each `notes/<stem>.json` sidecar from scratch
  through the same prompt and dedup as the pipeline (glossary-aware
  categorized annotation, low-threshold gate, within-chapter dedup,
  cross-chapter gap rule vs `tn_history.json` — a term
  annotated within `tn_gap_chapters` in an earlier chapter stays
  suppressed — and the same code-enforced `max_notes_per_chapter` cap with
  severity-ordered truncation, after which the kept notes are sorted into
  reading order by line index); the discarded candidates are recorded the
  same way too (`notes/<stem>.dropped.json`, review artifact only, and a
  pure category change counts as a change). An annotator response of
  zero notes for a chapter that HAS notes is treated as a failed
  evaluation, not an empty result: the sidecar and the translated
  markdown are left untouched (`[tn] <file>: annotator returned 0 notes
  for a chapter with N note(s) - keeping existing sidecar`). Chapter
  prose is never rewritten except the one-time
  stripping of legacy baked-in notes/markers, then the epub rebuilds unless
  `--no-build` (`--dry-run` still makes the annotator LLM calls but writes
  nothing). A re-check that changed at least one chapter — or that
  drifted only `tn_history.json` (kept notes bump `times`/`last_order`
  even when every sidecar is identical) — commits
  `tn: re-check notes`. Exit 0 success, 1 failed chapters (annotator call
  or unreadable chapter) or no eligible chapters in range, 2 usage error.

To audit the notes that already exist (no re-annotation, nothing
rewritten) — flagging notes that fail to earn their place, since the goal
of the whole system is notes that add context or explain context lost in
translation — run `review notes`:

- `uv run "$SCRIPT" review notes --project . [--chapters SPEC]
  [--batch-size N]` — audits every chapter with a `notes/<stem>.json`
  sidecar in manifest order (`--chapters SPEC` restricts, same SPEC
  semantics as `translate --chapters`; chapters without sidecars are
  skipped silently; a chapter whose translated or source file is missing
  or unreadable warns and skips). Each note's stored line index is re-resolved with the epub
  builder's anchor rules (the stored index wins while its translated line
  still starts with the stored anchor, else the first anchor-matching
  line wins, else the note is unresolvable); unresolvable notes become
  `misanchored` warns deterministically, without a model call. Resolvable
  notes are judged in `review_batch_size` batches (default 40;
  `--batch-size N` overrides) by the `reviewer` provider (temperature
  0.0) through `templates/notes_review.md`, each note paired with its
  translated line, the line-aligned source line, and the ±2-line target
  context. With a multi-model `reviewer` array each batch fans out to
  every model in parallel and merges via the consensus provider,
  announced by `[consensus] reviewer: {n} model(s) - merging results via
  the consensus provider`. A candidate that fails prints `[FAIL] consensus:
  reviewer candidate {i}/{n} ({model}) failed: {type}: {err}` and stops the
  run once every failure is named — as of v014 a provider failure is terminal
  (exit 3) rather than degrading to a survivor, in this tier exactly as in
  `translate`.
  Judgment kinds, a closed vocabulary: `restates` (the note adds
  nothing beyond what the translation already says), `overexplains`
  (common knowledge or inferable from context — fails the comprehension
  threshold), `wrong` (the note misexplains the source term),
  `misanchored` (attached to the wrong line or the term does not appear
  there). Advisory only: exit 0 on a completed run regardless of finding
  count — there is no `--fix` and no exit-1-on-warns (that is the
  glossary tier's contract; usage errors — `--fix`, `--batch-size` < 1,
  a bad `--chapters` spec, or any other flag that does not apply to the
  subject (`[FAIL] --<flag> does not apply to 'review <subject>'`) —
  exit 2) — and the report (same file as `review
  glossary`, config `review_report_path`, same per-run overwrite: a notes
  run replaces a previous glossary report and vice versa) carries NO
  `- Command:` bullets; findings are fixed by hand in
  `notes/<stem>.json` (delete the note, reword it, or re-attach it to
  the right line). The report footer states the caveat: hand-edited
  sidecars are overwritten if the `tn` re-check command later regenerates
  that chapter's notes. Console: `[notes] reviewing batch i/n` per batch,
  then `[ok] review notes: N findings (restates X, overexplains Y, wrong
  Z, misanchored W) -> <report path>` and, when N > 0, a `[warn]` hint
  that fixes are hand edits to `notes/<stem>.json`; a project with no
  sidecars anywhere prints `[ok] no chapter notes found - nothing to
  review` and writes no report — that `[ok]` means genuinely no sidecars,
  since a run whose chapters all got skipped (missing or unreadable
  files) prints `[warn] no chapter notes reviewed: N chapter(s) skipped
  (see failures above)` instead. Commits `review: notes audit` and logs one
  `notes_review` trace event.

## Whole-book glossary drift (`verify`)

Distinct from everything above. `review glossary` audits each entry on its own
merits; `verify` asks whether an entry **mistranslates where it is actually
used**, which is the failure that spreads: every chapter after the one that
seeded the term is translated against the same pinned rendering.

    uv run "$SCRIPT" verify --project <dir> [--out PATH]

Stage 1 is deterministic and costs nothing: it matches each translated
chapter's source against the glossary, then checks how the translation rendered
those terms — the balance rule the pipeline's advisory already prints, so a term
rendered fewer times than `max(1, ceil(min_term_coverage × occurrences))` is
flagged. A term rendered not at all is drift rather than under-use and is
left to the FAITH reviewer that already owns it.

Stage 2 spends calls **only on the terms stage 1 flagged**, on the `verify`
provider job (which inherits the translator's blocks until authored, and
arbitrates its own fan-out through `consensus_verify.md`). It batches **by term
across the book, not by chapter**: drift is "the same word meaning different
things in different contexts", and a per-chapter call structurally cannot see
one chapter's usage contradict another's. One call carries one term plus
**contexts** — the ±2-line window around a line the term occurs on, source and
translation paired at the **same line numbers** (index N is index N; that is the
line-indexed protocol VALIDATE enforces). A term with more than 8 contexts is
split across follow-up calls until every one is covered — a dropped context is
where a contaminating one hides — and the call count is printed. A verdict is
still per `(term, chapter)`, and a verdict naming a term or chapter that was not
offered is dropped.

Console markers: `[verify] <n> chapter(s) examined, <n> flagged ..., <n>
term/chapter finding(s) confirmed ... across <n> call(s)`, `[verify] report:
<path>`, `[verify] <chapter> -> needs-review: forcing '<term>' mistranslates this
chapter`, `[verify] glossary contamination: '<term>' pinned as '<pinned>'
mistranslates in <chapters> - ...`, `[warn] verify: <n> chapter(s) could not be
checked` details per failure, and `[ok] verify: no confirmed glossary drift`.
Writes to `logs/verify.jsonl` (`--out` overrides; a relative path resolves
against the project directory): one line per chapter **examined** — clean
chapters included with empty findings — plus one per contaminated entry, each
tagged `kind` (`chapter` or `contamination`).

**Never rewrites a translated chapter, and never writes `glossary.json`.** A
confirmed term flips its chapter to `needs-review` through `project.set_status`,
which is what makes the chapter eligible for `retry`; that happens on a stage-2
confirmation only, never on a stage-1 counting hit. The same confirmation also
flags the **glossary entry** as contaminated, on the console and in the report —
the chapter flip fixes the past, a corrected entry stops the next chapter — but
the flag is NOT stored in `glossary.json`, because that file feeds the
translator's pinned-terms list and a stored flag would change translation
behaviour. Exit 0 nothing confirmed, 1 something confirmed **or** a chapter that
could not be checked (an unreadable chapter, or one whose translation line count
differs from its source's, which the pairing cannot trust).

**The same scan runs automatically after a glossary change.** After
`review fix`, `glossary replace`, `glossary set`, `glossary merge` and
`glossary retire`, the CLI re-checks the chapters the edited term actually
occurs in — the deterministic, free scope computed with the same
`glossary.count_in_text` the balance advisory uses, matched against each
chapter's **source**, so chapters the edit cannot have reached are never
scanned and never paid for. A term that occurs in no chapter has no subject:
the command prints `[verify] cascade check after <verb>: '<term>' occurs in no
chapter - nothing to re-check` and makes no call.

Console markers for the automatic check are `[verify] cascade check after
<verb>: <n> chapter(s) carry the edited term, <n> flagged ..., <n> confirmed
across <n> call(s)`, `[warn] verify: <chapter> -> needs-review: forcing the
edited rendering mistranslates this chapter`, and the same
`[warn] verify: glossary contamination: ...` line the manual command prints.

The cascade check acts on a confirmation exactly as `verify` does: the chapter
is flipped to `needs-review`, through the same `project.set_status` path and
behind the same guard that saves the manifest only when something was
confirmed. `--strict` governs the **originating command's exit code only** —
never whether the flip happens — and `--no-verify` opts out of both the scan
and the flip. Both are accepted by `glossary replace | set | merge | retire`
and by `review fix`, where they are a usage error on the read-only `review
glossary` / `review notes` subjects. The check writes no `logs/verify.jsonl`:
that file is the manual command's, since `verify_project()` returns records
without writing anything and only `cmd_verify` renders the whole-book result
set, so a scoped cascade has no path to it. The automatic run is logged as a
`verify_cascade` event carrying its `origin`, `terms`, `chapters` and
`confirmed_chapters`.

`review fix` runs the check **once** for the whole batch, not once per applied
command: each spawned subcommand is invoked with `--no-verify` (the same
defensive pattern as the `--no-build` it already appends) and the executor
re-checks the union of the terms its applied commands actually wrote.

Note that retiring or merging a term away leaves the cascade check with
nothing to judge for **that** term — stage 1 matches against the live
glossary, and the term is no longer in it — so those two verbs usually report
`<n> flagged by the deterministic stage, 0 ... confirmed` while still scoping
the scan to the right chapters. The console says so rather than leaving the
`0` to read as a missing feature: `[verify] '<term>' is no longer a pinned
glossary term, so the deterministic stage has nothing to match it against;
those chapters were re-checked against the glossary as it now stands`. That is
the honest answer: an unpinned term cannot contaminate a future chapter.
Retiring a term still matters, because the chapters it was pinned in may now
contradict the remaining glossary.

### FAITH rejections

When the faithfulness gate rejects a translation, the pipeline also asks
whether the **glossary** was the cause — a different fault from the
translator's, and one fixed in `glossary.json` rather than by another attempt.
The gate is free and decisive: the chapter is dismissed unless it *also*
declined a pinned term the source matched — rendered below the usage floor, or
not at all — so the many rejections with nothing to do with the glossary cost
no model call at all. Both shapes count here, and a chapter that refused every
pinned form outright is the strongest over-binding candidate of all: it is the
shape a translator produces when the entry does not fit the sense, and no retry
can fix it while the entry stays pinned. Survivors go through the same stage 2,
on the same `verify` job, and are reported as `[warn] [CHAPTER_NNNN] glossary
over-binding: forcing '<term>' as '<pinned>' mistranslates this chapter;
fix the glossary entry, not the translation`, plus a
`faith_glossary_investigation` trace event. The investigation never rewrites
the rejected lines and never flips a status — the FAITH gate already owns the
chapter's outcome. Skip it with `--no-verify` on `translate` / `retry`.

## Bulk review fixes

`review glossary` may leave dozens of machine-determinable findings behind
(field-kind fixes with a suggestion, heuristic duplicate / variant
collisions with structured merge data, mundane terms to retire); applying
them one at a time by hand or by an agent is tedious and prone to drift.
For the offline machine-actionable path, run
`uv run "$SCRIPT" review fix --glossary review-report.md [--dry-run]
[--stale-ok] [--exit-on-error]`: it parses every `- Command:` bullet in the report's
`Machine-applicable` section and runs each as a subprocess (`glossary
replace | set | merge | retire`), in order, and exits 0 on full success or
full no-op, 1 if
any command failed (continues past failures by default; `--exit-on-error` to
stop at the first; a report whose `glossary_digest` no longer matches the
live glossary.json — the glossary changed since generation — is refused
with `[review fix] report is stale (glossary changed since generation) -
regenerate with review glossary` unless `--stale-ok`), 2 on a
missing/unreadable report or a report with no machine-applicable
commands. `review fix` accepts exactly `--glossary`
(the report path), `--dry-run`, `--stale-ok`, `--exit-on-error`, `--no-verify`
and `--strict`; any
other review
flag exits 2 with `[FAIL] --<flag> does not apply to 'review <subject>'`
(`--fix` on `review fix`/`review notes` keeps its own message:
`[FAIL] --fix applies to 'review glossary' only; not 'review <subject>'`)
— mind the naming trap: on `review fix`, `--glossary` is the report
path, not a glossary selector, so `review glossary --glossary X` is a
usage error. `review fix` makes no commit of its own — each
spawned glossary subcommand (`glossary set: <term>`,
`glossary merge: '<removed>' into '<kept>'`, `glossary retire: <term>`,
`glossary replace: '<src>' -> '<dst>'`) commits its own action.
Commands are pre-validated: a `glossary
replace`/`set` command whose `--translation` value contains source-script
(CJK) characters for a CJK-source entry is skipped in-process (console:
`[review fix] skipped [N]: suggestion not in target language` — the same
rule `review glossary --fix` already enforces), and a hand-written
`glossary set` command whose `--category` value is outside the category
vocabulary is skipped the same way (console: `[review fix] skipped [N]:
unknown category '<value>' (must be one of: place, person, org, skill,
technique, level, state, item, honorific, unit, other) (<command line>)` —
a glossary-independent check, so it fires even when glossary.json is
unreadable); both are excluded from the run
count, so skipped specs surface as findings that need a decision, not
runtime failures. A Command bullet carrying `--project` in either form
(`--project X` or `--project=X`) is likewise skipped (`[review fix]
skipped [N]: command overrides --project`) — the executor always prepends
its own `--project`, and one smuggled into a hand-edited report would
silently retarget the command. Conflicts are keyed per FIELD, not per
verb: a command claims the (field, resolved-source) keys of every field
it writes — `replace` edits the translation and shares `set
--translation`'s key (the two collide: two commands writing one field of
one entry is the exact pair that double-applies), a multi-field `set
--translation X --definition Y` occupies both keys, sources resolve
through glossary.find() so a command naming a variant and one naming the
canonical source collide — and a later command conflicting on ANY
claimed key is skipped whole; the first one queued wins
(`[review fix] skipped [N]: conflicting command
for '<source>' (already queued)`). Each spawned command is killed at a
1800s timeout (`[review fix] command timed out after 1800s: <command>`).
**The `- Command:` bullet is the contract**:
`write_report()` emits it on every finding whose fix is fully determined by
its structured fields, and `review fix` reads it; the closed vocabulary is
documented in `references/file-formats.md` (per-finding `glossary replace`
for mistranslation / wrong_language / collision with a suggestion;
`glossary set --definition` / `--category` for definition / category
findings with a suggestion; `glossary set --remove-variant` for heuristic
variants; `glossary merge --keep ... --remove ...` for heuristic
duplicates; `glossary retire --source S` for mundane findings — retiring
deletes the entry and appends the source to the top-level `retired` list,
so `seed` / GLOSSARY_EXPAND never re-add it). Legacy reports (the
pre-split format: no frontmatter, findings grouped by severity, possibly
no `- Command:` bullets, and the old `- Command:`
header that records the generating command) are synthesized on the fly from
the structured parts alone — no `review glossary` re-run needed. After
applying, one final `build-epub` runs when chapters changed and
`auto_build_epub` is on; the `- Command:` lines in the report never carry
`--no-build`, so they remain human-copyable.

Every glossary verb prints `[glossary] noop: <detail>` when a run changes
nothing (machine-detectable; `review fix` classifies these as no-ops).
The batch-flow subcommands:

- `uv run "$SCRIPT" glossary set --project . --source S [--translation T]
  [--definition D] [--category C] [--add-variant V] [--remove-variant V]
  [--alt-translations "A,B"] [--add-alt A] [--remove-alt A] [--no-verify]
  [--strict]` — atomic
  multi-field metadata edit (CJK-checked against the source like
  `apply_fixes`); idempotent; prints `[glossary] noop: <detail>` when
  nothing actually changes. Assigning `--category unit` to an entry with
  a non-empty translation warns `[warn] glossary: '<source>' has a
  translation but category 'unit' (guide-only: balance checks skip it)`.
- `uv run "$SCRIPT" glossary merge --project . --keep K --remove R [--no-verify]
  [--strict]` —
  transfer `variants` / `alt_translations` / `definition` from R to K
  (definition only fills K when K's is empty; a definition arriving for a
  K that already has one is discarded with `[warn] glossary: definition
  from '<removed>' discarded ('<kept>' already has one)`), append R to
  the top-level `retired` list, remove R from `terms`; idempotent
  (re-running with an already-retired R — canonical source or variant
  spelling — is a clean no-op exit 0, the supplied spelling recorded in
  `retired` alongside the canonical source). Translation / category /
  origin / first_seen_chapter of the kept entry are preserved.
- `uv run "$SCRIPT" glossary retire --project . --source X [--no-verify]
  [--strict]` — thin wrapper
  over `glossary.retire()` for a single source. Re-running with the
  entry's canonical source prints `[glossary] noop: already retired: X`
  and exits 0; a variant spelling of an already-retired entry is also a
  clean no-op (exit 0) — the supplied spelling is recorded in `retired`
  alongside the canonical source.
- `uv run "$SCRIPT" glossary replace --project . --source S
  --translation T [--keep-alt] [--no-build] [--dry-run] [--no-verify]
  [--strict]` — `--no-build`
  skips the auto epub build for batch runs; the replace behavior itself is
  unchanged. `--no-verify` / `--strict` govern the automatic cascade check
  described under Whole-book drift verification.
- `uv run "$SCRIPT" glossary search --project . TERM [--max-distance N]` —
  read-only lookup across `source`, `variants`, `translation`, and
  `alt_translations` (both sides) to find entries before the editing
  subcommands above: case-insensitive substring always hits, plus fuzzy
  Levenshtein within `--max-distance` (default: config
  `fuzzy_max_distance`; `0` = substring only)
  against whole values or single words, with no separator special-casing
  (`grand elder` finds `grand-elder`); retired matches print `[glossary]
  retired match: ...` info lines. Exit 0 with matches, 1 none, 2 error.
- `uv run "$SCRIPT" glossary count --project . TERM [--variants "A,B"]
  [--chapters SPEC] [--min N]` — read-only significance check: counts
  non-overlapping occurrences of TERM plus the comma-separated
  `--variants` across the source chapters (per-chapter breakdown,
  matched longest-first so a nickname inside a full name counts once);
  `--chapters SPEC` restricts the count (same SPEC semantics as
  `translate --chapters`) and `--min N` overrides the threshold
  (default: config `min_term_occurrences`). Exit 0 at or above the
  threshold, 1 below it (`[warn] '<term>' is below the significance
  threshold: <total> < <min>`), 2 usage error.
