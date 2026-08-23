---
name: novel-translator
description: Multi-pass CJK novel translation orchestrator. Scaffolds translation projects, seeds and grows a term glossary, translates chapters through a staged pipeline (line-indexed JSON translation, glossary-balance check, model faithfulness gate, deduplicated translation notes) against a self-hosted sglang/OpenAI-compatible endpoint, and exports epub3 ebooks validated with epubcheck. Use whenever the user mentions translating novels or web-novel chapters, setting up or resuming a translation project, translation glossaries, translation notes, or building/fixing translated epubs — even for casual asks like "translate the next few chapters" or "rebuild the epub".
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

The user provides `source/Chapter_NNNN.md` files (4-digit zero-padded; extras
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
gradient background) into `covers/cover.jpg`, generates a **style profile**
(one model call over a random sample of source chapters →
`novel_info.json:style_profile`, feeds every later prompt; skip with
`--skip-profile`, regenerate with `profile`), and seeds the glossary from the
skill's catalogues: every catalogue term appearing ≥ `seed_min_count`
(default 3) times across all source chapters is added. Refuses to overwrite
an existing project unless `--force`.

Optional: `--cover-url` to point at a cover image directly.

### 3. Translate chapters (`translate`)

```bash
uv run "$SCRIPT" translate --project . --chapters 5-20   # inclusive range; also "42" or "5-10,30"
uv run "$SCRIPT" translate --project . --next 10          # first 10 pending chapters in order
```

Sequential by design — the glossary is meant to grow as you go, so later
chapters translate more consistently than earlier ones. Already-`translated`
chapters are skipped; `--force` retranslates anyway.

Each chapter runs through a state machine (resumable; safe to Ctrl-C and
rerun the same command):

1. **PREP** — split the source into an indexed JSON line array; build the
   *contextual glossary* (only glossary terms actually appearing in this
   chapter, capped at 80, sorted by frequency).
2. **TRANSLATE** — fill `templates/translation.md`, call the `translator`
   provider with the WHOLE chapter (maximum context; only chapters estimated
   above `translate_chunk_max_tokens`, default 20k, split into balanced
   parts). The numbered-line protocol plus the corrective retry keep the
   one-line-in/one-line-out contract intact. Output caps are dynamic per
   call (~1.6x estimated input).
3. **VALIDATE** — line count must match the source exactly; empty lines stay
   empty. Structural violations go back as feedback.
4. **BALANCE** — for every contextual glossary term: count occurrences in
   source vs translation (fuzzy, Levenshtein ≤ 2). Deviations become feedback.
5. **GLOSSARY_EXPAND** — the model proposes new drift-prone terms (names,
   places, skills, orgs...); identical duplicates are skipped, conflicting
   ones are merged by the model into the existing entry.
6. **FAITH** — the `reviewer` provider judges faithfulness line by line.
   FAILURE reasons become feedback.
7. **TN_GENERATE / TN_DEDUP** — the `annotator` provider proposes translation
   notes with line indices; a note is kept only if the term wasn't annotated
   within the last `tn_gap_chapters` (default 10) chapters (`tn_history.json`).
8. **ASSEMBLE** — write `translated/Chapter_NNNN.md` with epub3-ready
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
  entry), or tweak `templates/`, then
  `uv run "$SCRIPT" retry --project . --chapters 7`.
- **Translate by hand**: write the final chapter to
  `translated/Chapter_0007.md` following the translated-chapter format
  (frontmatter + one paragraph per line + `[^N]` markers + TN section), then
  `uv run "$SCRIPT" mark --project . --chapters 7 --status translated`.

`translate --next` deliberately skips `needs-review` chapters.

### 4. Build the epub (`build-epub`)

```bash
uv run "$SCRIPT" build-epub --project .            # add --skip-check to skip validation
```

Builds `export/<title-slug>.epub`: flat epub3 TOC (one entry per chapter,
sorted by manifest order), metadata from `novel_info.json`, cover from
`covers/`, translation notes as real epub3 footnotes
(`epub:type="noteref"`/`"footnote"`), then validates with the epubcheck
docker image. The build fails loudly if epubcheck reports errors — show the
report to the user and fix the chapter(s) named in it. To run epubcheck
manually from Git Bash:

```bash
MSYS_NO_PATHCONV=1 docker run --rm -v "C:\path\to\export:/data" epubcheck /data/book.epub
```

## Operating notes

- **The tool is fully manual-runnable** — every operation is one of the
  commands above; the agent only saves typing. `README.md` in this skill is
  the human-facing cheat sheet.
- **Prompts follow the Hy-MT2 model card conventions**: full language names,
  `X translates to "Y"` terminology pairs, structured-data preservation
  rules for the numbered-line protocol, `[Background Information]` context
  frames (novel style profile + previous-chunk tail), and the cultural-
  adaptation threshold pattern for translation notes. Keep new template
  edits aligned with those patterns.
- **Providers are per job** in `config.json` (`translator`, `glossary`,
  `reviewer`, `annotator`, `profile`). Start with one endpoint doing
  everything; later point `reviewer` at a stronger model without touching
  the rest. `ping` shows what each job resolves to. Temperature and top_p
  are per-provider passthroughs (translator defaults 0.7/1.0 per the model
  card — quality across temperatures is subjective; the user tunes this).
- **Templates are per project** (`templates/`). Tuning a prompt for a
  specific novel is normal — edit, then `retry` the affected chapters.
- **Glossary upkeep**: hand-fix bad entries any time; the balance check reads
  `glossary.json` fresh for every chapter. Re-run seeding with
  `uv run "$SCRIPT" seed --project .` after adding chapters or editing a
  catalogue.
- **New source language**: drop a catalogue JSON with the right `language`
  field into the skill's `assets/catalogues/` (see file-formats.md), pass
  `--source-lang` at init. The pipeline itself is language-agnostic.
- **Cost/cadence**: each chapter ≈ 4 model calls + retries. `status` before
  long batches; run `ping` first if the server was restarted.
- Script output is plain ASCII on purpose (`[ok]`/`[FAIL]` markers) — parse
  it, don't guess. Exit code is non-zero when any chapter ends
  `needs-review`.
