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

- **Files provided**: the user hands you `source/Chapter_NNNN.md` files
  (1-4 digit zero-padded; extras get a letter suffix, `Chapter_0042a.md`).
  Frontmatter is optional - `init` backfills `novel_title`/`author`/
  `source_url` and derives `chapter_title` from the first body line. Never
  renumber or rename chapter files; the pipeline depends on the naming
  scheme.
- **URL given (the common case)**: scrape the table of contents for the
  ordered chapter list, fetch each chapter with the environment's web
  tooling (e.g. firecrawl), and write UTF-8 `source/Chapter_NNNN.md` files
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

Downloading more chapters later? Drop them in `source/` and run
`uv run "$SCRIPT" sync --project .` - it re-scans `source/`, backfills
their frontmatter, and rebuilds `chapters.json` (added/removed chapters
reported; statuses and titles preserved) without touching glossary.json
or tn_history.json. Exit 0, no LLM calls; a sync that changed anything
commits `sync: rescan source`; run it after every download batch.

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
enters it: `[Chapter_NNNN] [warn] <file>: source chapter has no content -
marked needs-review` — the chapter is marked `needs-review` outright,
with no LLM call. Per-attempt preparation happens inline before the
state machine rather than being a resumable stage: every attempt splits the
source into an indexed JSON line array and builds the *contextual glossary*
(only glossary terms actually appearing in this chapter, capped at 200,
sorted by frequency):

1. **TRANSLATE** — fill `templates/translation.md`, call the `translator`
   provider with the WHOLE chapter when its expected output fits the
   one-call budget `floor(0.8 × translate_max_output_tokens) − 256`
   (the cap defaults to 8k, the model card's recommended output range);
   longer chapters split by greedy per-line token-budget packing (parts
   close past that same budget, so every part takes at least one line),
   each still carrying style
   background and the previous part's tail as input context. The
   numbered-line protocol plus the corrective retry keep the
   one-line-in/one-line-out contract intact; a part whose response looks
   truncated (missing line indices / cut JSON) retries once at an escalated
   cap (~1.5x, never above the provider's `max_tokens`), other shape
   problems at the same cap. A provider `max_tokens` below
   `translate_max_output_tokens` warns once per run (`[warn] config:
   providers.translator.max_tokens (N) is below translate_max_output_tokens
   (M) - retries cannot raise the output cap`). A source line whose
   estimated output exceeds
   even the escalated cap fails fast with actionable feedback
   (`TRANSLATE failed: ... source line N alone exceeds the output budget
   (estimated X tokens > Y cap); split or shorten the line manually`)
   before any model call. Validated parts are persisted as they complete
   (`chunks` in the draft state), so a crash or Ctrl-C mid-TRANSLATE
   resumes at the next part
   (`[Chapter_NNNN] [init] resuming translation at part k/n (m lines
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
   <M>)` — updates, merges, and nickname absorption of entries already
   in the glossary are never gated. Proposed categories are validated
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
   `[Chapter_NNNN] [ok] notes: K kept (cats); D dropped (reasons) -> notes/<stem>.dropped.json`
   (`0 kept` omits the parenthetical; a zero-drop chapter ends at the kept
   clause).
7. **ASSEMBLE** — write `translated/Chapter_NNNN.md` as clean markdown (no
   footnote markers, no notes section) plus the `notes/<stem>.json` sidecar
   carrying the kept notes; auto-promote. The manifest status update is
   best-effort: after retrying through Windows file-lock contention
   (exponential backoff, ~3.1s in total) it gives up with `[warn] manifest
   update failed for <file>: <reason> - chapter file is written; status
   stays in-progress` — the chapter file is complete, only the status
   stays `in-progress`. On gate failure the chapter retried
   up to `max_attempts` (default 3) with all accumulated feedback and the
   rejected translation injected into each retry; then it becomes
   `needs-review`.

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
cat draft/Chapter_0007.state.json                  # full accumulated gate feedback
```

Read the feedback, then choose:
- **Fix the cause and retry**: adjust `glossary.json` (e.g., a term whose
  English translation is awkward to use verbatim — add an `alt_translations`
  entry; to propagate a changed translation to chapters already translated,
  `glossary replace` beats retranslating — see Glossary upkeep), or tweak
  `templates/`, then
  `uv run "$SCRIPT" retry --project . --chapters 7` — or
  `retry --failed` to retry every needs-review chapter at once (selection is
  by status, so hand-marked chapters are included).
- **Translate by hand**: write the final chapter to
  `translated/Chapter_0007.md` following the translated-chapter format
  (frontmatter + one paragraph per line; translator's notes, if any, go in
  the `notes/Chapter_0007.json` sidecar — see
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
timed out after 300s`; so does a docker infrastructure failure — daemon
down, image or volume missing — which is also reported as `epubcheck
skipped`, never as a validation failure).
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
`=== epub build after Chapter_NNNN.md | timestamp ===` separators; the console
prints `[epub-auto] build ok (after Chapter_NNNN.md)`. Failures are warnings
only and never change the translate/retry exit code (which still reflects
translation status): a stalled build is killed after 360s — the kill takes the whole process tree, so the builder's own docker run cannot outlive it (`[warn] epub
auto-build stalled, killed after 360s (after <reason>) - see
logs/epub-build.log`), a failed child prints `[warn] epub auto-build
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
  `reviewer`, `annotator`, `recap`, `profile`; the `recap` job generates
  the rolling story-so-far recap — a cheap model is a good fit — and the
  `profile` job only serves `--style auto`). Start with one endpoint doing
  everything; later point `reviewer` at a stronger model without touching
  the rest. `ping` shows what each job resolves to. Temperature and top_p
  are per-provider passthroughs (translator defaults 0.7/1.0 per the model
  card — quality across temperatures is subjective; the user tunes this).
  A per-provider `thinking` flag defaults to false (sglang
  `chat_template_kwargs.enable_thinking`) so hybrid-thinking models spend
  the output budget on the answer. Hosted providers authenticate with
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
  reads as drifted. A template that exists but
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
- **Glossary upkeep**: hand-fix bad entries any time (`glossary search` is
  the read-only lookup — see Bulk review fixes); the balance check reads
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
  through `templates/glossary_review.md`. Console sequence: the header
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
  source from glossary.json's `retired` list to allow re-adding. Re-run
  seeding with `uv run "$SCRIPT" seed --project .` after adding chapters or
  editing a catalogue; `--min-count N` overrides the seed threshold for the
  run, and `--catalogue PATH` (repeatable) adds an explicit catalogue file,
bypassing the language filter. Every mutating action in this bullet
commits — `seed: N glossary term(s)`, `review: glossary audit` (suffixed
`review: glossary audit (N fix(es) applied)` when `--fix` landed fixes),
`glossary replace` / `util replace: '<src>' -> '<dst>'` — while
read-only `glossary search` / `glossary count` commit nothing.
- **New source language**: drop a catalogue JSON with the right `language`
  field into the skill's `assets/catalogues/` (see file-formats.md), pass
  `--source-lang` at init. The pipeline itself is language-agnostic.
- **Cost/cadence**: each chapter ≈ 5 model calls + retries (translate —
  one call per part on longer chapters — faithfulness, glossary
  expansion, notes, story recap), plus a cleanup-judgment call only when
  balance flags drift signals; one glossary
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
  severity-ordered truncation); the discarded candidates are recorded the
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
  context. Judgment kinds, a closed vocabulary: `restates` (the note adds
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
(the report path), `--dry-run`, `--stale-ok`, and `--exit-on-error`; any
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
rule `review glossary --fix` already enforces) and excluded from the run
count, so skipped specs surface as findings that need a decision, not
runtime failures. A Command bullet carrying `--project` in either form
(`--project X` or `--project=X`) is likewise skipped (`[review fix]
skipped [N]: command overrides --project`) — the executor always prepends
its own `--project`, and one smuggled into a hand-edited report would
silently retarget the command. Two commands targeting the same glossary
entry with the same verb+target conflict (a `glossary set` verb is its
edited field, so two `set` commands on different fields of one entry both
run) — the first one queued wins,
later ones are skipped (`[review fix] skipped [N]: conflicting command
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
  [--alt-translations "A,B"] [--add-alt A] [--remove-alt A]` — atomic
  multi-field metadata edit (CJK-checked against the source like
  `apply_fixes`); idempotent; prints `[glossary] noop: <detail>` when
  nothing actually changes. Assigning `--category unit` to an entry with
  a non-empty translation warns `[warn] glossary: '<source>' has a
  translation but category 'unit' (guide-only: balance checks skip it)`.
- `uv run "$SCRIPT" glossary merge --project . --keep K --remove R` —
  transfer `variants` / `alt_translations` / `definition` from R to K
  (definition only fills K when K's is empty; a definition arriving for a
  K that already has one is discarded with `[warn] glossary: definition
  from '<removed>' discarded ('<kept>' already has one)`), append R to
  the top-level `retired` list, remove R from `terms`; idempotent
  (re-running with an already-retired R — canonical source or variant
  spelling — is a clean no-op exit 0, the supplied spelling recorded in
  `retired` alongside the canonical source). Translation / category /
  origin / first_seen_chapter of the kept entry are preserved.
- `uv run "$SCRIPT" glossary retire --project . --source X` — thin wrapper
  over `glossary.retire()` for a single source. Re-running with the
  entry's canonical source prints `[glossary] noop: already retired: X`
  and exits 0; a variant spelling of an already-retired entry is also a
  clean no-op (exit 0) — the supplied spelling is recorded in `retired`
  alongside the canonical source.
- `uv run "$SCRIPT" glossary replace --project . --source S
  --translation T [--keep-alt] [--no-build] [--dry-run]` — `--no-build`
  skips the auto epub build for batch runs; the replace behavior itself is
  unchanged.
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
