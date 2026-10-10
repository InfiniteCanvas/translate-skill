---
name: novel-translator
description: Multi-pass CJK novel translation orchestrator. Scaffolds translation projects, seeds and grows a term glossary, translates chapters through a staged pipeline (line-indexed JSON translation, advisory glossary-balance signals with a model faithfulness gate, deduplicated translation notes) against a self-hosted sglang/OpenAI-compatible endpoint, reviews glossary and translation-note quality, and exports epub3 ebooks validated with epubcheck. Use whenever the user mentions translating novels or web-novel chapters, setting up or resuming a translation project, downloading or scraping a novel from the web to set up a translation project, translation glossaries, glossary quality, translation notes, re-evaluating or auditing translator's notes, or building/fixing translated epubs — even for casual asks like "translate the next few chapters", "review the glossary", "recheck the translation notes", or "rebuild the epub".
---

# Novel Translator

Orchestrates translation of CJK (currently Chinese) novel chapters into
English through a self-hosted sglang endpoint, keeping terminology consistent
across hundreds of chapters via a growing glossary, attaching deduplicated
translation notes, and exporting epub3 ebooks.

Everything runs through one CLI (self-contained via uv, no venv setup needed):

```bash
SCRIPT="<this skill's directory>/scripts/translate.py"
uv run "$SCRIPT" <subcommand> --project <project-dir>   # --project defaults to cwd
```

Read `references/file-formats.md` before editing any project file by hand —
it defines every schema (glossary, manifest, state files, translated-chapter
markdown contract).

## Prerequisites

- `uv` on PATH (scripts declare their dependencies inline via PEP 723).
- Docker with an image named `epubcheck` (optional — validates built
  epubs; without it the epubcheck step is skipped with a warning and
  builds still succeed).
- The sglang endpoint reachable (check with `uv run "$SCRIPT" ping`).

## Project lifecycle

### 1. Acquire and prepare source material

Two ways source chapters arrive:

- **Files provided**: the user hands you `source/CHAPTER_NNNN.md` files
  (exactly 4 zero-padded digits, no suffix). Any other shape is not
  discovered — but `init`/`sync`
  print a `[warn]` naming each ignored file and why, so read that output
  before reporting a chapter count back to the user.
  Frontmatter is optional - `init` backfills `novel_title`/`author`/
  `source_url` and derives `chapter_title` from the first body line. Never
  renumber or rename chapter files; the pipeline depends on the naming
  scheme.
- **URL given (the common case)**: scrape the table of contents for the
  ordered chapter list, fetch each chapter with the environment's web
  tooling (e.g. firecrawl), and write UTF-8 `source/CHAPTER_NNNN.md` files
  - one paragraph per physical line, per-chapter frontmatter with
  `chapter_title` + `source_url` when the site provides them - then `init`.
  For long novels, work in batches: init on the first batch, then after
  each new batch run `sync` (see below) and `translate --next N`.

Full conversion rules, naming pitfalls, and a verification checklist are in
`references/ingestion.md` - read it before ingesting from a URL.

### 2. Initialize (`init`)

```bash
uv run "$SCRIPT" init --project . \
  --title "凡人修仙传" --author "忘语" \
  --source-url "https://example.com/novel" \
  --source-lang zh --target-lang en \
  --tags "xianxia,cultivation" \
  --api-base "http://100.85.218.125:8888/v1"
```

Creates the folder structure, `config.json`, `novel_info.json`,
`chapters.json`, empty `glossary.json`/`tn_history.json`, copies the prompt
templates into `templates/`, backfills frontmatter on bare source chapters,
scrapes a cover from the source URL (`og:image`) or generates one (title on a
gradient background) into `covers/cover.jpg`, copies the chosen **style
guide** to `style.md` (preset-based, zero LLM calls; see below), and seeds
the glossary from the
skill's catalogues: every catalogue term appearing ≥ `seed_min_count`
(default 3) times across all source chapters is added. It also turns the
project directory into a git repository (console
`[git] initialized repository`) and makes the initial
`init: scaffold project` commit -- the bare chapters, taken before any
rewrite -- followed by `init: backfill and seed` once the frontmatter,
manifest and glossary are in place; every later mutating action commits too
(subject table in `references/file-formats.md` § Git history). On a
machine without git, init still succeeds but prints
`[warn] git not found; project history disabled`, and in a project dir
that is already a repository the skill did not create — no
`.git/novel-translator-managed` marker, and a `.gitignore` that is not the
skill's rules alone — commits are skipped, with one
`[warn] git: skipping commits - ...` line per run (details in
file-formats.md § Git history).
Refuses to overwrite an existing project unless `--force`; the
`glossary.json`/`tn_history.json`/`story_state.json` reset happens only
under `--force` (resetting the first two to empty and deleting
`story_state.json`; the per-chapter `notes/` sidecars survive with their
translated chapters). Re-running `init` without `--force` on a directory
whose `config.json` was deleted preserves all three files and prints
`[init] preserving existing glossary.json, tn_history.json, and
story_state.json (pass --force to reset)`.

Optional: `--cover-url` to point at a cover image directly.

If the skill ships a `config.local.json` (it is gitignored, so you will only
ever have one if you created it), `init` deep-merges it into the new
`config.json` automatically — your endpoint, model and auth land in every
project without retyping them, and everything the overlay does not mention
(languages, thresholds, the `version` stamp) is left as `init` wrote it.
Nothing changes when the file is absent; a malformed one warns and is skipped
without failing `init`. See `sync-config` below to re-apply it later.

Downloading more chapters later? Drop them in `source/` and run
`uv run "$SCRIPT" sync --project .` - it re-scans `source/`, backfills
their frontmatter, and rebuilds `chapters.json` (added/removed chapters
reported; statuses and titles preserved) without touching glossary.json
or tn_history.json. Exit 0, no LLM calls; a sync that changed anything
commits `sync: rescan source`; run it after every download batch.

### Sharing settings across projects (`sync-config`)

```bash
uv run "$SCRIPT" sync-config --project .
```

Deep-merges the skill's `config.local.json` into this project's
`config.json` — the same pass `init` does — so a project created before you
wrote the file (or one whose endpoint you have since changed) picks up your
current settings. Overlay keys win; every other key in the project file is
preserved, including the `version` stamp, so this is safe to re-run
(idempotent: a second run reports `no changes`). An absent overlay is a
clean no-op (`[ok] no local config at ... - nothing to sync`, exit 0); an
unreadable or malformed one fails with exit 2 rather than silently leaving
the project on the wrong endpoint. Commits as `sync-config: apply local
config`.

Merge rules worth knowing: scalars and objects merge key-by-key, but a
`providers.<job>` given as an **array** replaces that job's blocks
wholesale, while a single block **object** merges into every existing block.
So `{"providers": {"translator": {"model": "x"}}}` re-points only the model
and keeps the project's `base_url`/auth, while a full array replaces the
whole job (this is how a different endpoint gets swapped in). The overlay
may not carry `version` — that stamp belongs to `init`/`migrate` alone and
is rejected if present.

**Secrets:** an inline `"api_key": "sk-..."` in the overlay is gitignored in
this repo, but merging it puts it into the project's `config.json`, which the
project repository *does* commit — `sync-config` prints a `[warn]` naming
every such key. Prefer `"api_key_env": "MY_KEY"` (the key then stays in the
environment and never reaches any file).

Re-run seeding with `uv run "$SCRIPT" seed --project .` after adding
chapters or editing a catalogue; `--min-count N` overrides the seed
threshold for the run, and `--catalogue PATH` (repeatable) adds an
explicit catalogue file, bypassing the language filter (commit subject:
`seed: N glossary term(s)`).

**Style selection** (`--style NAME|PATH|auto`, default `classic`): a preset
name copies that guide from the skill's `assets/styles/` to
`<project>/style.md` and records `"style": NAME` in `novel_info.json` —
zero LLM calls. Presets: `classic` (measured, concise literary English for
standard xianxia/wuxia), `transmigration` (modern protagonist voice +
internet memes against a classic cultivation world), `modern`
(contemporary settings, dialogue-forward English), `literary` (elevated
epic register). A filesystem path to a .md file does the same with the file
stem as the name; a project can override/add presets by dropping .md files
into `<project>/styles/`; the `styles` subcommand lists presets (name +
description). **Choose the style deliberately after skimming a few source
chapters**: `transmigration` when the protagonist is from modern Earth
(isekai/transmigration tropes, system prompts, meme usage in the source),
`classic` for standard xianxia/wuxia, `literary` for deliberately lush
prose, `modern` for contemporary settings; when unsure after skimming,
`classic`. `--background "..."` records 2-4 sentences of novel context as
`novel_info.json:background` (fed to every prompt's
`[Background Information]` frame) — if it wasn't set at init, fill it from
the novel's synopsis/source page by editing `novel_info.json` before
translating. `style.md` is hand-editable; edits apply on the next
translate without re-init. `--style auto` is the legacy model-generated
profile path (`novel_info.json:style_profile`, one LLM call over a random
sample of source chapters; regenerate with the `profile` subcommand, whose
`--chapters N` / `--chars N` flags override the sample size) for novels
that fit no preset; `--skip-profile` is a no-op for preset styles,
and with `--style auto` it skips the profile LLM call.
`status` prints the active style line.

### 3. Translate chapters (`translate`)

```bash
uv run "$SCRIPT" translate --project . --chapters 5-20   # inclusive range; also "42" or "5-10,30"
uv run "$SCRIPT" translate --project . --next 10          # first 10 pending or in-progress chapters in order
```

Sequential by design — the glossary is meant to grow as you go, so later
chapters translate more consistently than earlier ones. Already-`translated`
chapters are skipped; `--force` retranslates anyway.

Each chapter runs through a state machine (resumable; safe to Ctrl-C and
rerun the same command). A source chapter with no content lines never
enters it: `[CHAPTER_NNNN] [warn] <file>: source chapter has no content -
marked needs-review` — the chapter is marked `needs-review` outright,
with no LLM call. Per-attempt preparation happens inline before the
state machine rather than being a resumable stage: every attempt splits the
source into an indexed JSON line array and builds the *contextual glossary*
(only glossary terms actually appearing in this chapter, capped at 200,
sorted by frequency):

1. **TRANSLATE** — fill `templates/translation.md`, call the `translator`
   provider with the WHOLE chapter when its expected output fits the
   one-call budget `floor(0.8 × translate_max_output_tokens) − 256`
   (the cap defaults to 64k since v009, raised from 8k so a reasoning
   model drawing on the same budget cannot truncate the answer);
   longer chapters split by greedy per-line token-budget packing (parts
   close past that same budget, so every part takes at least one line),
   each still carrying style
   background and the previous part's tail as input context. The
   numbered-line protocol plus the corrective retry keep the
   one-line-in/one-line-out contract intact; a part whose response looks
   truncated (missing line indices / cut JSON) retries once at an escalated
   cap (`max(ceiling, min(~1.5x pack_cap, the smallest `max_tokens` across the
   translator's blocks))`, other shape
   problems at the same cap. Since v012 `translate_max_output_tokens` is a
   CEILING, not an override: each translator block's `max_tokens` is a hard
   provider limit the pipeline may lower but never raise, the sent value is
   `min(ceiling, that block's max_tokens)` per block, and chapters pack to
   `min(ceiling, smallest block)`. A block below the ceiling therefore wins and
   is supported — it draws a once-per-run note, not a warning
   (`[info] config: translation output cap is N, not
   translate_max_output_tokens (M) ...`; with a multi-block
   translator array, N is the smallest `max_tokens` across blocks — the
   minimum governs packing so no model truncates its part). Two `[warn]` lines
   still apply: a translator block below 8192 cannot be packed into, and a
   `reasoning_effort` high/xhigh/max block that the ceiling squeezes below its
   own `max_tokens` may stop converging. As of v015 a config that fans out at
   all without an authored `providers.consensus` is refused outright at load
   (exit 2) rather than warned about — with nothing authored, the arbitrator
   would be silently derived from `providers.translator[0]`, i.e. one of the
   models it is judging. A config where nothing fans out needs none. A source line whose
   estimated output exceeds
   even the escalated cap fails fast with actionable feedback
   (`TRANSLATE failed: ... source line N alone exceeds the output budget
   (estimated X tokens > Y cap); split or shorten the line manually`)
   before any model call. Validated parts are persisted as they complete
   (`chunks` in the draft state), so a crash or Ctrl-C mid-TRANSLATE
   resumes at the next part
   (`[CHAPTER_NNNN] [init] resuming translation at part k/n (m lines
   already done)`; a packing that no longer matches the saved parts —
   source or config changed — discards them with a `[warn]` and restarts
   from scratch).
2. **VALIDATE** — line count must match the source exactly; empty lines stay
   empty. Structural violations go back as feedback.
3. **BALANCE** — for every contextual glossary term: count occurrences in
   source vs translation (Levenshtein tolerance ≤ 2 on single-word targets
   of ≥ 5 letters; multi-word phrases match case-insensitively with
   hyphen/space equivalence plus an optional inflection on the final word;
   cross-entry longest-first, so a term nested inside a longer glossary
   compound — e.g. 仙界 inside 修仙界 — is credited to the longer term
   only). Fully advisory: nothing fails here. Entries in a guide-only
   category (`unit`) are skipped entirely — they are injected into the
   translation prompt as rendering guides, but their short polysemous
   source strings (里 in 这里/里面, 寸 in idioms) make counting pure noise,
   and emitting no signals also puts them beyond auto-cleanup retirement.
   Entries with no `translation` yet (minimal hand-added stubs) are skipped
   too — without a canonical rendering there is nothing to enforce.
   All three tiers (drift
   signals, under-use warnings, over-count info) surface as
   `balance_advisory` trace events; only drift signals and under-use
   warnings also print console `[warn]`s — under-use warnings capped at
   the first 5 per chapter (`[warn] ... and N more (see logs)`
   summarizes the rest), drift signals one `[warn]` each, over-count
   findings trace-log only.
   Drift signals (canonical rendering absent while the term appears ≥2× in
   the source) first run the same cleanup judgment as before (one
   `glossary`-provider call, `templates/glossary_cleanup.md`): KEEP named
   entities and named actions (people/places/sects/techniques/named
   artifacts/named realms, plus titles bound to a name — "Empress Dowager
   Zhao", "Steward Li"; "Empress Dowager" alone never qualifies), REMOVE
   class nouns (everyday words, common nouns/verbs, generic objects,
   transient phrases, standalone titles, kinship terms, and other ways one
   character addresses another — "great grandmother", "big brother").
   The retirement itself is DEFERRED: flagged terms are only dropped from
   `glossary.json` into its `retired` list after FAITH accepts the
   translation (applied alongside GLOSSARY_EXPAND; console: `[glossary]
   retired mundane term '...' (<reason>)`; trace event `glossary_cleanup`) — a
   rejected attempt retires nothing, since a bad translation is exactly
   what produces false drift. Retired terms are never re-added by `seed`
   or GLOSSARY_EXPAND. Kept signals are appended to the FAITH reviewer's
   prompt (see stage 4), which owns the verdict; cleanup errors are
   fail-safe (`[warn] glossary cleanup failed - keeping all signals: <error>`).
4. **FAITH** — the `reviewer` provider judges faithfulness line by line.
   The reviewer also receives any kept BALANCE drift signals as heuristic
   term-consistency flags: it fails on genuine terminology drift but not on
   legitimate counting false positives (nested compounds,
   inflections/hyphenations, generic words). FAILURE reasons become
   feedback.
5. **GLOSSARY_EXPAND** — the model proposes new terms that are named
   entities or named actions (personal names and their aliases, places,
   orgs, named artifacts, technique & skill names, named realms/states,
   plus titles bound to a name — "Empress Dowager Zhao" qualifies,
   "Empress Dowager" alone does not; a term must name one specific
   referent, and what one character calls another — kinship terms and
   other ways of address such as "great grandmother", "big brother",
   "my lord" — never qualifies, even for one specific person, any more
   than class nouns like 麦穗 "wheat stalks"); identical duplicates are
   skipped, conflicting
   ones are merged by the model into the existing entry. A brand-new
   proposal must also clear a significance gate: it is counted across the
   whole source corpus and added only at >= `min_term_occurrences`
   occurrences (default 3; 0 disables the gate, and an unreadable source
   corpus fails it open), skipped below the floor with
   `[glossary] skip '<src>' - <N> occurrence(s) across the novel (min
   <M>)` — the gate also runs before nickname absorption, so an absorbed
   nickname is novel-wide significant too, and any NEW variant a
   re-proposal carries for an existing entry is gated the same way and
   dropped below the floor with `[glossary] skip variant '<v>' - <N>
   occurrence(s) across the novel (min <M>)`. Never gated: the source of
   a matched entry (matched by source or variant), already-present
   variants being restated, and real merges of a matched entry.
   Proposed categories are validated
   against the known glossary categories (the model is offered the list
   minus `unit` — units come from catalogues, not proposals): a known
   category — `unit` included — passes unchanged, an unknown one is
   coerced to `other` with `[glossary] warn unknown category '<cat>' for
   '<source>' - coerced to 'other'`, and the same coercion covers
   category updates the merge model returns for existing entries.
   Assigning `unit` to an entry that already has a translation warns
   (`[warn] glossary: '<source>' has a translation but category 'unit'
   (guide-only: balance checks skip it)`); `glossary set --category unit`
   warns the same way. Runs
   only on the
   attempt FAITH just accepted — new terms lock in after the translation is
   accepted, never from a rejected one (a chapter that ends needs-review
   adds no terms). BALANCE's deferred retirements are applied here first,
   on the same acceptance gate.
6. **TN_GENERATE / TN_DEDUP** — the `annotator` provider proposes translation
   notes with line indices, glossary-aware (the prompt pins the glossary's
   deliberate renderings so transliterated or nuance-dropping renderings get
   a note for what the source term literally carries) and categorized
   (`cultural` | `idiom` | `wordplay` | `honorific` | `unit` | `other`;
   a missing/unknown category silently becomes `other`). Self-assessed
   low-confidence (`threshold: "low"`) notes are dropped by default (set
   `tn_keep_low_confidence` true to keep them); a note is kept only if the
   term wasn't annotated within the last `tn_gap_chapters` (default 10)
   chapters (`tn_history.json`). The `max_notes_per_chapter` cap is enforced
   in code after the gap rule — the prompt asks for severity-ordered entries
   (wordplay and idioms first, then references/allusions, then honorific
   nuances), so truncation keeps the most severe context loss. Every
   discard (low threshold, cap overflow, invalid entry) is recorded in the
   `notes/<stem>.dropped.json` review artifact (the epub builder does not
   read it), and the stage prints
   `[CHAPTER_NNNN] [ok] notes: K kept (cats); D dropped (reasons) -> notes/<stem>.dropped.json`
   (`0 kept` omits the parenthetical; a zero-drop chapter ends at the kept
   clause).
7. **ASSEMBLE** — write `translated/CHAPTER_NNNN.md` as clean markdown (no
   footnote markers, no notes section) plus the `notes/<stem>.json` sidecar
   carrying the kept notes; auto-promote. The manifest status update is
   best-effort: after retrying through Windows file-lock contention
   (exponential backoff, ~6.3s in total) it gives up with `[warn] manifest
   update failed for <file>: <reason> - chapter file is written; status
   stays in-progress` — the chapter file is complete, only the status
   stays `in-progress`. On gate failure the chapter retried
   up to `max_attempts` (default 3) with all accumulated feedback and the
   rejected translation injected into each retry; then it becomes
   `needs-review`.

**Multi-model consensus**: every provider call the pipeline makes —
TRANSLATE, FAITH, GLOSSARY_EXPAND, the glossary merge/cleanup judgments,
TN_GENERATE, and the recap below — goes through the job's provider array
in `config.json`. A single-block job makes exactly one call; a job with
two or more blocks fans the prompt out to all its models in parallel and
ONE arbitrator call merges the labeled candidates into the
final response under the task's own JSON schema, so gates and validators
see an ordinary single-model reply (console: `[consensus] {job}: {n}
model(s) - merging results via the consensus provider`). That arbitrator is
resolved PER JOB (v015): `providers.consensus_<job>` if authored, else the
global `providers.consensus` — so the translation merge and the notes merge can
use different models, and a project that authors no per-job key is unchanged.
**As of v014 a
provider failure is terminal rather than degrading**: any call that fails after
its retries stops the run — one `[FAIL]` line, exit 3 — instead of falling back
to a surviving candidate or a lone one, and a code the provider itself calls
irrecoverable (Z.AI auth / balance / invalid-parameter) skips the retry ladder
entirely. A run that cannot reach its provider should say so rather than emit a
chapter that looks translated; the ladder is in
`references/file-formats.md` § Exit codes.
The merge is the one call allowed past its own block's `max_tokens` (an
arbitrator set to 65536 still merges full-size candidates), so a provider block
whose endpoint would reject the result should declare `max_tokens_limit` — its
hard ceiling. That key is never overridden, and it also drives the packing
budget, so chapters are never sized for a response the provider will truncate.

**Rolling story recap**: every chapter also maintains a running "story so
far" in `story_state.json` (one entry per chapter). After ASSEMBLE, one
`recap`-provider call (`templates/recap.md` — a job meant for a cheap
model) condenses the previous recap plus the just-finished chapter into a
≤ 120-word recap, and the next chapter's translate / faithfulness /
note-annotate prompts receive it inside their `[Background Information]`
frame, so the model translating chapter N knows the plot of chapters
1..N-1. A predecessor without an entry (chapters translated before the
feature, or a crash between assemble and record) is backfilled with one
extra call before translation starts. The recap is advisory context,
never a gate: failures only warn (`[warn] recap ...`) and the chapter
proceeds without it. Retranslating a chapter refreshes only its own entry;
later entries stay as built until those chapters are themselves
retranslated.

Gates pass → the chapter is accepted automatically. The user fixes residue by
hand if any turns up later. Each finished chapter is committed —
`translate: chapter NNNN (translated)` or
`translate: chapter NNNN (needs-review)`; `retry` commits the same way, and
skipped chapters commit nothing.

### Handling `needs-review` chapters

```bash
uv run "$SCRIPT" status --project .                # see statuses + attempt counts
uv run "$SCRIPT" status --why --project .          # + why each needs-review chapter is stuck
cat draft/CHAPTER_0007.state.json                  # full accumulated gate feedback
```

Read the feedback, then choose:
- **Fix the cause and retry**: adjust `glossary.json` (e.g., a term whose
  English translation is awkward to use verbatim — add an `alt_translations`
  entry; to propagate a changed translation to chapters already translated,
  `glossary replace` beats retranslating — see `references/maintenance.md`),
  or tweak `templates/`, then
  `uv run "$SCRIPT" retry --project . --chapters 7` — or
  `retry --failed` to retry every needs-review chapter at once (selection is
  by status, so hand-marked chapters are included).
- **Translate by hand**: write the final chapter to
  `translated/CHAPTER_0007.md` following the translated-chapter format
  (frontmatter + one paragraph per line; translator's notes, if any, go in
  the `notes/CHAPTER_0007.json` sidecar — see
  `references/file-formats.md`), then
  `uv run "$SCRIPT" mark --project . --chapters 7 --status translated`
  (committed as `mark: <file> -> <status>[, ...]`).

`translate --next` deliberately skips `needs-review` chapters.

### 4. Build the epub (`build-epub`)

```bash
uv run "$SCRIPT" build-epub --project .            # add --skip-check to skip validation
```

Builds `export/<title-slug>.epub` (filename from `novel_info.json`'s
`title_translated` when set — set it for an English filename; a CJK-only
title is preserved verbatim, with a console hint): flat epub3 TOC (one entry per chapter,
sorted by manifest order), metadata from `novel_info.json`, cover from
`covers/`, translation notes as real epub3 footnotes
(`epub:type="noteref"`/`"footnote"`, rendered from each chapter's
`notes/<stem>.json` sidecar, with a fallback to legacy markers baked into
the markdown), then validates with the epubcheck
docker image (when Docker/epubcheck is unavailable the check degrades
gracefully — `[warn] docker not found; epubcheck skipped` followed by
`[warn] epubcheck skipped`, and the build still succeeds with exit 0; an
epubcheck run exceeding 300s degrades the same way — `[warn] epubcheck
timed out after 300s`; so does a recognized docker infrastructure
failure — exit 125, or the common exit-1 patterns: an unreachable daemon
(including daemon-socket permission denials), a missing local image,
platform manifest mismatches, credential-helper failures, and the
registry pull refusals/rate limits — which is also reported as
`epubcheck skipped`, never as a validation failure; the pattern list is
best-effort, not exhaustive, so an unrecognized docker failure can still
be reported as a validation failure, exit 1).
The build fails loudly if epubcheck reports errors — show the
report to the user and fix the chapter(s) named in it. To run epubcheck
manually from Git Bash:

```bash
MSYS_NO_PATHCONV=1 docker run --rm -v "C:\path\to\export:/data" epubcheck /data/book.epub
```

**Auto-build**: during `translate`/`retry` the epub is rebuilt
automatically — each chapter that reaches `translated` fires a background
`build-epub` subprocess (builds run one at a time; triggers arriving
mid-build coalesce into the next build), and a final synchronous build at
batch end guarantees the finished epub includes every chapter. `export/`
thus always holds a current epub (epubcheck-validated when Docker is
available) — no manual builds
during long batches. Child build output (incl. epubcheck results) appends
to `logs/epub-build.log` with
`=== epub build after CHAPTER_NNNN.md | timestamp ===` separators; the console
prints `[epub-auto] build ok (after CHAPTER_NNNN.md)`. Failures are warnings
only and never change the translate/retry exit code (which still reflects
translation status): a stalled build is killed after 360s — the kill
takes the builder's whole process tree (taskkill /T on Windows, killpg
elsewhere), which reaches the docker CLI and its children, but the
daemon-side epubcheck container is not the builder's child and may run
to completion — docker `--rm` reaps it when it exits (`[warn] epub
auto-build stalled, killed after 360s (after <reason>) - see
logs/epub-build.log`; on a confirmed kill the dead build's pid-scoped
`export/*.epub.<pid>.tmp` sibling is swept — `[warn] removed N stale
epub temp file(s) left by the killed build`). A kill that itself fails
warns `[warn] epub auto-build: failed to kill builder (taskkill:
<error>) - it may still be running` (killpg on POSIX) and a builder
that survives the kill warns `[warn] epub auto-build builder survived
the kill - it may still be running` — neither raises, and neither
changes the exit code. A failed child prints `[warn] epub auto-build
failed, exit <code> (after <reason>) - see logs/epub-build.log`, a Ctrl-C
interrupt prints `[warn] epub auto-build interrupted`, and the
end-of-batch finalize can report `[warn] epub auto-build finalize waited
<n>s for the builder to exit`. When epubcheck is unavailable the rebuild
is skipped with `[warn] epub auto-build could not run (epubcheck
unavailable) - skipping validation` rather than surfacing as a failed
validation. Set `auto_build_epub: false` (default true) in
`config.json` to build only via the command above; Ctrl-C kills a running
background build too. Builds produce no git commits — `export/`,
`logs/`, and `covers/` are gitignored.

## Operating notes

- **The tool is fully manual-runnable** — every operation is one of the
  commands above; the agent only saves typing. `README.md` in this skill is
  the human-facing cheat sheet.
- **Prompts follow the Hy-MT2 model card conventions**: full language names,
  `X translates to "Y"` terminology pairs, structured-data preservation
  rules for the numbered-line protocol, `[Background Information]` context
  frames (novel background + rolling story recap + previous-chunk tail),
  and the cultural-
  adaptation threshold pattern for translation notes. Keep new template
  edits aligned with those patterns.
- **Providers are per job** in `config.json` (`translator`, `glossary`,
  `reviewer`, `annotator`, `recap`, `profile`, `consensus`; the `recap` job
  generates the rolling story-so-far recap — a cheap model is a good fit —
  the `profile` job only serves `--style auto`, and `consensus` merges
  multi-model candidates). Each job's value is an ARRAY of provider blocks
  (a bare block object is the legacy single-model shape and loads
  unchanged); two or more blocks fan the job's prompts out to all models
  in parallel and merge via that job's arbitrator (see the multi-model
  note under the pipeline). An omitted job inherits the translator's whole
  array element-wise (authored keys win, the job's sampling defaults
  apply); `consensus` never inherits the array — exactly one block,
  defaulting to the translator's first block (its authored keys win;
  temperature 0.2 unless it sets one). As of v015 a fan-out job may also name
  its own arbitrator with an optional `consensus_<job>` sibling key
  (`consensus_translator`, `consensus_annotator`, …), which merges KEY-WISE
  onto the global block — so `{"model": "x"}` keeps the global's endpoint and
  auth — while an ABSENT key resolves to the global block unchanged. Start
  with one endpoint doing everything; later point `reviewer` at a stronger
  model, or add a second translator block, without touching the rest.
  `ping` shows what each job resolves to, one line per block: single-block
  jobs keep the bare padded job name, while a multi-block job's blocks are
  index-suffixed (`[ok] translator[0] <url> -> <model> (config model:
  <m>)`) and a repeated model inside one job warns (`[warn]
  translator[i]: same model as translator[j] (<model>) - candidates will
  be near-identical`). An in-force per-job arbitrator is probed too, labelled
  `[ok] consensus(translator) <url> -> <model>`, and deduplicated against the
  blocks already probed. Temperature and top_p
  are per-provider passthroughs (translator defaults 0.7/1.0 per the model
  card — quality across temperatures is subjective; the user tunes this).
  A per-provider `thinking` flag defaults to false (sglang
  `chat_template_kwargs.enable_thinking`) so hybrid-thinking models spend
  the output budget on the answer. That field is **sglang-only** — hosted
  APIs ignore it, so a hosted reasoning model still thinks at full depth
  and can burn the whole budget without answering. Bound it in
  `extra_body` instead (`reasoning_effort`, or `thinking: {type:
  "disabled"}` where supported); see `config.local.EXAMPLES.md`.
  Hosted providers authenticate with
  `api_key_env` (environment variable name) or `api_key` on the provider
  block.
- **Templates are per project** (`templates/`). Tuning a prompt for a
  specific novel is normal — edit, then `retry` the affected chapters.
  Template files missing from a project's `templates/` dir fall back to
  the skill's `assets/templates/`, so newly shipped templates work in old
  projects.
- **Upgrading projects (`migrate`)**: `uv run "$SCRIPT" migrate --project .
  [--dry-run] [--force]` upgrades an existing project to the current
  skill version — non-destructive, re-runnable, and it never resets
  `glossary.json`/`tn_history.json`/`story_state.json` (unlike
  `init --force`). Steps live
  in `scripts/migrations/` as `v001.py`, `v002.py`, ... — one module per
  version, zero-padded so sort order = version order; the chain is
  discovered from the files present (current version = highest), so a
  skill update that needs project-side changes ships a new
  `scripts/migrations/v<NNN>.py` and nothing else. The project's
  position is the top-level integer `version` in `config.json` —
  written by `init` (fresh projects are born current) and by `migrate`
  after each successfully applied step (immediate per-step stamping, so
  a crash mid-chain resumes at the failed step; each applied step is
  committed after its stamp, subject `migrate: vNNN <DESCRIPTION>`),
  never merged from defaults; absent = 0. `migrate` runs only steps with a higher version,
  in order. v001 materializes merged config
  defaults onto disk (keys introduced after the project's init, e.g.
  `glossary_auto_cleanup`, `min_term_coverage`, plus provider-job
  normalization; user-set values always preserved) and copies shipped
  templates missing from the project's `templates/` dir without
  prompting (non-destructive; the runtime fallback would cover them
  anyway). v002 materializes the review command defaults
  (`review_batch_size`, `review_report_path`) the same add-only way
  (user-set values always preserved). v003 materializes the
  `git_commits` default the same add-only way and turns the project
  directory into a git repository (DESCRIPTION: `git history:
  materialize git_commits default, init the project repo`; the step
  itself commits nothing — the per-step commit after the stamp captures
  the fully migrated state). v004 materializes the `min_term_occurrences`
  default the same add-only way (DESCRIPTION: `add min_term_occurrences
  (novel-wide significance gate for glossary expansion)`). v005 rewords
  the shipped glossary templates so glossary terms are restricted to
  named entities, named actions, and titles bound to a name — templates
  only, no config change (DESCRIPTION: `restrict glossary terms to named
  entities, named actions, and name-bound titles`). v006 backs the
  guide-only unit category — templates only, no config change
  (DESCRIPTION: `transliterated measurement units: conversion-note
  guidance and the guide-only unit category`): `tn_generate.md` gains a
  standing exception requiring a conversion note at a transliterated
  unit's first chapter occurrence, and `glossary_review.md` exempts
  `category: "unit"` entries from the mundane judgment. v007 lands the
  rolling-recap batch (DESCRIPTION: `materialize the recap provider job;
  ship recap.md and notes_review.md; refresh tn_generate.md`): the new
  `recap` provider block is materialized into config.json (no new
  top-level key — the omitted job inherits the `translator` block), and
  the template sync ships the two new templates (`recap.md`, the rolling
  story recap prompt; `notes_review.md`, the `review notes` tier) plus
  the rewritten `tn_generate.md` — a pre-v007 project's copy always
  reads as drifted. v008 lands the provider-array batch (DESCRIPTION:
  `provider arrays + the consensus job (multi-model consensus); ship
  consensus.md`): every `providers.<job>` value is normalized to an
  array of blocks (a legacy single-block dict wraps into a one-element
  array; user-set keys kept verbatim) and the new `consensus` job is
  materialized (an omitted job still inherits the translator's list;
  consensus itself is exactly one block), with the template sync
  shipping the new `consensus.md` synthesis prompt. v009 raises the
  translation output budget (DESCRIPTION: `raise the translation output
  budget to 64k (translate_max_output_tokens 8192->65536 and provider
  max_tokens 16384->65536, together)`): both numbers move in the same step,
  since the truncation-retry cap is clamped to the smallest translator
  `max_tokens` and raising only the translate cap would make the retry
  smaller than the attempt it retries. Only values still equal to the old
  default are rewritten, so custom numbers survive. v010 renames source
  chapters to the fixed 4-digit, all-caps canonical form (DESCRIPTION: `rename
  source chapters to the fixed 4-digit, all-caps canonical form
  (Chapter_001.md -> CHAPTER_0001.md), carrying chapters.json,
  story_state.json and the per-chapter artifacts`): the old regex accepted
  1-4 digits, any case, and an optional letter suffix, so a project with
  `Chapter_001.md` or
  `Chapter_0042a.md` silently loses those chapters after the tightening — no
  manifest entry, no status, no translation target. v010 renames the source
  file **and every artifact keyed on its name** (`translated/<file>`,
  `draft/<stem>.*`, `notes/<stem>*`, `chapters.json`'s `file`,
  `story_state.json`'s recap keys); artifacts move before the manifest is
  rewritten, so a mid-way failure leaves the manifest pointing at names that
  still exist. An existing target is never clobbered. **Extra chapters — a
  letter suffix — are deliberately NOT renamed**: they produce a `[warn]`
  naming each file and telling the operator to hand the decision to their
  agent, because with the suffix gone the mechanical rename would be
  `Chapter_0042a.md` → `CHAPTER_0042.md`, clobbering a real chapter. If you see
  that warning, read the TOC, give each extra chapter its own number, rename to
  `CHAPTER_NNNN.md`, and update `chapters.json`, `story_state.json`, `draft/`,
  `translated/` and `notes/` to match. v012 is report-only (DESCRIPTION: `decouple
  translate_max_output_tokens from provider max_tokens (the key is now a ceiling
  a block can lower, not an override; report mismatched translator pairs)`): it
  rewrites nothing and only explains a mismatched pair, because a resolution
  change has no old-default sentinel to rewrite against. Where it reports one,
  a chapter already mid-translation re-packs to the tightest block and restarts —
  tell the operator before resuming a batch. v013 is report-only too
  (DESCRIPTION: `provider max_tokens_limit: a block may declare its provider's
  hard output ceiling, which binds the consensus synthesis too; report a
  consensus block the merge would exceed`): it adds no key and rewrites nothing,
  and reports the number the consensus merge would send when it exceeds that
  block's own `max_tokens` and no `max_tokens_limit` is declared. Tell the
  operator to add the key if their provider rejects that many — as of v014 that
  failure is an immediate `exit 3`, not a silent degrade to one candidate. v014
  is report-only as well (DESCRIPTION: `a provider failure that exhausts its
  retries is now fatal (exit 3) instead of degrading quietly, and Z.AI
  irrecoverable codes skip the retry ladder; report provider blocks with no
  usable credential`): it rewrites nothing and only reports a provider block
  with no usable credential, which would now stop the run at its first call.
  An unset `api_key_env` in the shell running `migrate` is a `[warn]`, not a
  refusal — the operator's real shell usually has it. A template that
  exists but
  differs from the shipped one is
  prompted for interactively, one prompt per template:
  `templates ~ <name>.md differs from the shipped copy - overwrite
  it? [y/N]` — Enter/n keeps the project's version (the default; it
  may be a user customization), y overwrites it with the shipped
  copy. `--force`
  answers yes to all prompts (no prompting); non-interactive runs
  (stdin not a TTY: piped, scripted, CI) never prompt and never
  block — differing templates are kept with a `[warn]` line.
  `--dry-run` reports differing templates without prompting or
  writing. A project already current no longer just reports the
  version and exits: `migrate` runs a read-only-when-clean template
  maintenance pass (same prompt rules — missing templates copied,
  differing ones prompted / `--force`-refreshed) that leaves the
  version stamp untouched, so `migrate --force` on a current project
  refreshes stale templates instead of silently doing nothing (a pass
  that changed files commits `migrate: refresh templates`; a missing
  repository — a v003 whose `git init` failed once — is backfilled,
  committing `migrate: backfill git repository` when only the repo was
  created). The
  chain still gates structural steps; template refresh is maintenance,
  not a chain step. Exit 0 ok/no-op, 2 usage error (missing
  config.json, missing skill templates dir, unreadable version key,
  broken chain, project newer than the skill).
- **Glossary and notes maintenance**: hand-fixes to `glossary.json` are
  safe any time — the balance check reads it fresh for every chapter. The
  maintenance workflows — `review glossary` (entry audit, `--fix`),
  `review notes` + `tn` (translator's-note quality and re-check),
  `review fix` (apply a review report), `glossary
  set|merge|retire|replace|search|count`, `util replace` — are documented
  in `references/maintenance.md`; read it before running any of them, or
  when the user asks to audit, fix, merge/dedupe, or retire glossary
  terms, propagate a changed translation, re-check notes, walk through
  `review-report.md` findings, change auto-retirement
  (`glossary_auto_cleanup`, the `retired` list), or check a term's
  significance (`glossary count`).
- **Reading the trace logs**: every run writes two tiers under `logs/` —
  an orchestration timeline (`logs/run-<run_id>.jsonl`: run and chapter
  lifecycle, stage transitions, gate verdicts, degradations, one `llm_call`
  summary per model call, never a body) and the model IO for each chapter
  (`logs/chapters/CHAPTER_NNNN/run-<run_id>.jsonl`), each with an
  append-only `index.jsonl` whose unclosed `open` line marks an invocation
  that died, plus a `report.md` per chapter. Run `uv run "$SCRIPT" logs` for
  the newest run's timeline, `uv run "$SCRIPT" logs <chapter>` for that
  chapter's model IO, `--list` to enumerate runs, `--json` for a clean
  stream, `--report [--io]` to regenerate a report with model bodies. A
  whole-project dashboard is written to `logs/report.html` at the end of every
  run and rebuildable with `uv run "$SCRIPT" logs --html`; prefer it when the question is about the
  project as a whole — which chapters are slow, where the tokens went, what is
  unclosed, what each model actually returned — and read the page rather than the JSONL. Read
  this when the user asks why a chapter failed, what a model actually
  returned, what a gate rejected, where the token cost went, or what happened
  before a crash.
- **New source language**: drop a catalogue JSON with the right `language`
  field into the skill's `assets/catalogues/` (see file-formats.md), pass
  `--source-lang` at init. The pipeline itself is language-agnostic.
- **Cost/cadence**: each chapter ≈ 5 model calls + retries (translate —
  one call per part on longer chapters — faithfulness, glossary
  expansion, notes, story recap), plus a cleanup-judgment call only when
  balance flags drift signals; a job whose provider array carries N
  models multiplies its calls by N plus one consensus call per fan-out;
  one glossary
  review pass ≈ ceil(N/review_batch_size) calls for N entries (40 by
  default), one notes review pass ≈ ceil(M/review_batch_size) calls for M
  notes. `status` before long
  batches; run `ping` first if the server was restarted.
- Script output is UTF-8 (CJK terms appear in glossary/replace/search
  lines) with stable `[ok]`/`[FAIL]`/`[warn]`/`[git]` markers prefixing
  status lines — parse the markers, don't guess. Non-zero exit is not
  just the needs-review case: `references/file-formats.md` § Exit codes
  enumerates them per command (e.g. `build-epub` exits 1 when epubcheck
  fails validation but 2 when the builder crashes; `ping` exits 2 when
  providers are unreachable). Usage/setup errors — bad arguments, missing
  files, corrupt project JSON, a numeric config key set to a non-numeric
  value or null (`[FAIL] config key '<key>' must be a number (got
  <value!r>)`) — print one `[FAIL]` line and exit 2, never a raw
  traceback.
