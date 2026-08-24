---
name: novel-translator
description: Multi-pass CJK novel translation orchestrator. Scaffolds translation projects, seeds and grows a term glossary, translates chapters through a staged pipeline (line-indexed JSON translation, advisory glossary-balance signals with a model faithfulness gate, deduplicated translation notes) against a self-hosted sglang/OpenAI-compatible endpoint, reviews glossary quality, and exports epub3 ebooks validated with epubcheck. Use whenever the user mentions translating novels or web-novel chapters, setting up or resuming a translation project, translation glossaries, glossary quality, translation notes, or building/fixing translated epubs — even for casual asks like "translate the next few chapters", "review the glossary", or "rebuild the epub".
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
- Docker with an image named `epubcheck` (used to validate built epubs).
- The sglang endpoint reachable (check with `uv run "$SCRIPT" ping`).

## Project lifecycle

### 1. Prepare source material

The user provides `source/Chapter_NNNN.md` files (1-4 digit zero-padded; extras
get a letter suffix: `Chapter_0042a.md`). Each file has YAML frontmatter with
`source_url`, `novel_title`, `chapter_title`, `author`. If chapters arrive
without frontmatter, create it from the info the user gives you — `init`
fills `order` automatically. Do not renumber or rename chapter files; the
pipeline depends on the naming scheme.

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
(default 3) times across all source chapters is added. Refuses to overwrite
an existing project unless `--force`.

Optional: `--cover-url` to point at a cover image directly.

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
uv run "$SCRIPT" translate --project . --next 10          # first 10 pending chapters in order
```

Sequential by design — the glossary is meant to grow as you go, so later
chapters translate more consistently than earlier ones. Already-`translated`
chapters are skipped; `--force` retranslates anyway.

Each chapter runs through a state machine (resumable; safe to Ctrl-C and
rerun the same command). Per-attempt preparation happens inline before the
state machine rather than being a resumable stage: every attempt splits the
source into an indexed JSON line array and builds the *contextual glossary*
(only glossary terms actually appearing in this chapter, capped at 200,
sorted by frequency):

1. **TRANSLATE** — fill `templates/translation.md`, call the `translator`
   provider with the WHOLE chapter when its expected output fits
   `translate_max_output_tokens` (default 8k, the model card's recommended
   output range); longer chapters split into parts sized to that output cap,
   each still carrying style background and the previous part's tail as
   input context. The numbered-line protocol plus the corrective retry keep
   the one-line-in/one-line-out contract intact.
2. **VALIDATE** — line count must match the source exactly; empty lines stay
   empty. Structural violations go back as feedback.
3. **BALANCE** — for every contextual glossary term: count occurrences in
   source vs translation (fuzzy, Levenshtein ≤ 2; cross-entry longest-first,
   so a term nested inside a longer glossary compound — e.g. 仙界 inside
   修仙界 — is credited to the longer term only). Fully advisory: nothing
   fails here. All three tiers (drift signals, under-use warnings, over-count
   info) surface as `balance_advisory` trace events; only drift signals and
   under-use warnings also print console `[warn]`s (over-count is
   trace-only).
   Drift signals (canonical rendering absent while the term appears ≥2× in
   the source) first run the same cleanup judgment as before (one
   `glossary`-provider call, `templates/glossary_cleanup.md`): KEEP terms
   whose consistent rendering matters (names of people/places/sects/
   techniques/titles/artifacts/cultivation realms, culturally loaded
   concepts), REMOVE mundane ones (everyday words, common nouns/verbs,
   generic objects, transient phrases whose natural translation varies).
   The retirement itself is DEFERRED: flagged terms are only dropped from
   `glossary.json` into its `retired` list after FAITH accepts the
   translation (applied alongside GLOSSARY_EXPAND; console: `[glossary]
   retired mundane term '...'`; trace event `glossary_cleanup`) — a
   rejected attempt retires nothing, since a bad translation is exactly
   what produces false drift. Retired terms are never re-added by `seed`
   or GLOSSARY_EXPAND. Kept signals are appended to the FAITH reviewer's
   prompt (see stage 4), which owns the verdict; cleanup errors are
   fail-safe (`[warn] glossary cleanup failed - keeping all signals`).
4. **FAITH** — the `reviewer` provider judges faithfulness line by line.
   The reviewer also receives any kept BALANCE drift signals as heuristic
   term-consistency flags: it fails on genuine terminology drift but not on
   legitimate counting false positives (nested compounds,
   inflections/hyphenations, generic words). FAILURE reasons become
   feedback.
5. **GLOSSARY_EXPAND** — the model proposes new drift-prone terms (names,
   places, skills, orgs...); identical duplicates are skipped, conflicting
   ones are merged by the model into the existing entry. Runs only on the
   attempt FAITH just accepted — new terms lock in after the translation is
   accepted, never from a rejected one (a chapter that ends needs-review
   adds no terms). BALANCE's deferred retirements are applied here first,
   on the same acceptance gate.
6. **TN_GENERATE / TN_DEDUP** — the `annotator` provider proposes translation
   notes with line indices; self-assessed low-confidence (`threshold: "low"`)
   notes are dropped by default (set `tn_keep_low_confidence` true to keep
   them); a note is kept only if the term wasn't annotated
   within the last `tn_gap_chapters` (default 10) chapters (`tn_history.json`).
7. **ASSEMBLE** — write `translated/Chapter_NNNN.md` with epub3-ready
   footnote markers; auto-promote. On gate failure the chapter retried up to
   `max_attempts` (default 3) with all accumulated feedback injected into each
   retry; then it becomes `needs-review`.

Gates pass → the chapter is accepted automatically. The user fixes residue by
hand if any turns up later.

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
  (frontmatter + one paragraph per line + `[^N]` markers + TN section), then
  `uv run "$SCRIPT" mark --project . --chapters 7 --status translated`.

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
(`epub:type="noteref"`/`"footnote"`), then validates with the epubcheck
docker image. The build fails loudly if epubcheck reports errors — show the
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
thus always holds a current, epubcheck-validated epub — no manual builds
during long batches. Child build output (incl. epubcheck results) appends
to `logs/epub-build.log` with
`=== epub build after Chapter_NNNN.md | timestamp ===` separators; the console
prints `[epub-auto] build ok (after Chapter_NNNN.md)`. Failures are warnings
only and never change the translate/retry exit code (which still reflects
translation status). Set `auto_build_epub: false` (default true) in
`config.json` to build only via the command above; Ctrl-C kills a running
background build too.

## Operating notes

- **The tool is fully manual-runnable** — every operation is one of the
  commands above; the agent only saves typing. `README.md` in this skill is
  the human-facing cheat sheet.
- **Prompts follow the Hy-MT2 model card conventions**: full language names,
  `X translates to "Y"` terminology pairs, structured-data preservation
  rules for the numbered-line protocol, `[Background Information]` context
  frames (novel background + previous-chunk tail), and the cultural-
  adaptation threshold pattern for translation notes. Keep new template
  edits aligned with those patterns.
- **Providers are per job** in `config.json` (`translator`, `glossary`,
  `reviewer`, `annotator`, `profile`; the `profile` job only serves
  `--style auto`). Start with one endpoint doing
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
- **Glossary upkeep**: hand-fix bad entries any time; the balance check reads
  `glossary.json` fresh for every chapter. Nothing in the pipeline audits
  ENTRY quality — `uv run "$SCRIPT" review glossary --project .` does: a
  deterministic heuristic tier (cross-entry duplicate/variant collisions;
  a translation shared by other entries (info); translation still in the
  source language or equal to the source; unknown category; non-CJK text
  in a CJK entry's variants (info)) plus the `glossary` provider judging
  source-translation alignment, definitions, categories, and cross-entry
  conflicts in batches of 40 (`--batch-size N`) through
  `templates/glossary_review.md`. Report-only: one `[glossary] warn|info`
  line per finding, never touching glossary.json unless `--fix`; `--fix`
  applies only model-suggested fixes (direct model-tier warn findings,
  or a suggestion the merge borrowed onto a heuristic finding), to
  translation/definition/category only, validated (valid category, no
  source-language text), skipping conflicting suggestions (console:
  `[glossary] fixed '<source>': <field> '<old>' -> '<new>'` per change).
  Exit 0 clean or info-only (or every warn fixed), 1 warns remain,
  2 usage/setup error; empty glossary exits 0. Cost ceil(N/40) model
  calls; model-tier failures fail safe per batch — heuristic findings
  still report. Every run also writes indexed `<project>/review-report.md`
  (overwritten each run, clean runs too; console: `[glossary] report: <path>`)
  — numbered OUTSTANDING findings only, each with the full entry JSON + an
  Action line (model-written when available, else a per-kind template);
  tell an agent "fix items 1,4,5 in review-report.md doing what was
  suggested". When a glossary translation changes (hand edit or `review
  glossary --fix`), already-translated chapters still carry the old
  rendering — fix them with `glossary replace`, not expensive retranslation
  (`retry --chapters N`): `uv run "$SCRIPT" glossary replace --project .
  --source 灵根 --translation "spiritual root"` finds the entry by source
  or variants (exit 2 when unknown), sets `translation` in place, then
  rewrites the old rendering across chapters — a friendly no-op (exit 0)
  when the translation already equals the new one; by default it also
  prunes the old rendering from the entry's `alt_translations` (a stale
  alt would let the balance check keep counting the old rendering as
  valid, masking drift), while `--keep-alt` leaves alt_translations
  untouched for renderings that should stay accepted variants.
  `uv run "$SCRIPT" util replace --project . --source "spirit root"
  --target "spiritual root"` is the raw-phrase variant for arbitrary term
  fixes. Both are offline (no LLM calls) and match smartly, mirroring the
  balance checker's phrase semantics minus the fuzzy tier: case-insensitive,
  words may be joined by whitespace or hyphens, optional inflection
  (es/s/ed/ing) on the last word — each match's capitalization is
  preserved and the inflection re-appended (Spirit root → Spiritual root;
  spirit roots → spiritual roots); CJK phrases replace as exact literal
  substrings; footnote markers `[^N]` are never disturbed. Only the chapter
  body is rewritten (everything after the frontmatter, Translator's Notes
  included; frontmatter stays byte-verbatim; only files with matches are
  rewritten, atomically, LF); chapters are enumerated from the manifest
  (status `translated`), listed-but-missing files reported as warnings.
  `--dry-run` reports the glossary diff and per-chapter occurrence counts,
  writing nothing. Console: `[util] replace '...' -> '...'` /
  `[glossary] '灵根' translation: 'old' -> 'new' (pruned alt: ...)`
  headers, `[replace] Chapter_NNNN.md: N occurrence(s)` per changed
  chapter, `[ok] replaced X occurrence(s) in Y chapter(s) (Z scanned)`,
  `[warn] no occurrences found ...` (still exit 0). Exit codes: 0 success,
  2 usage/setup (no manifest, unknown glossary term). After any chapter
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
  bypassing the language filter.
- **New source language**: drop a catalogue JSON with the right `language`
  field into the skill's `assets/catalogues/` (see file-formats.md), pass
  `--source-lang` at init. The pipeline itself is language-agnostic.
- **Cost/cadence**: each chapter ≈ 4 model calls + retries; one glossary
  review pass ≈ ceil(N/40) calls for N entries. `status` before long
  batches; run `ping` first if the server was restarted.
- Script output is plain ASCII on purpose (`[ok]`/`[FAIL]` markers) — parse
  it, don't guess. Exit code is non-zero when any chapter ends
  `needs-review`.
