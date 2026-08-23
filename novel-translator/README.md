# novel-translator

A staged, resumable novel-translation tool. It drives a self-hosted
OpenAI-compatible endpoint (sglang) through multiple passes per chapter --
line-indexed translation, glossary-consistency checks, a model faithfulness
gate, translator's notes -- while a project-local glossary keeps names and
terms consistent across the whole book. Finished chapters assemble into a
validated epub3.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) -- runs the CLI with its inline dependencies
- Docker with an `epubcheck` image available (EPUB validation; only needed
  by `build-epub`, or pass `--skip-check`)
- A running sglang endpoint serving an OpenAI-compatible `/v1` API

All commands look like this:

    uv run <path-to>/novel-translator/scripts/translate.py <command> --project <dir>

`--project` defaults to the current directory and may also be given before
the subcommand.

## One-time setup

1. Make a project directory and put source chapters in `source/` named
   `Chapter_NNN[a].md` (1-4 digit zero-padded number, optional single-letter
   suffix, e.g. `Chapter_001.md`, `Chapter_0002a.md`). Frontmatter is
   optional; `init` backfills novel-level fields and chapter titles.

2. Initialize the project:

       uv run scripts/translate.py init --project . \
           --title "<original title>" --author "<author>" \
           --source-url "<url>" --source-lang zh --target-lang en

   This writes `config.json`, `novel_info.json`, an empty `glossary.json`
   and `tn_history.json`, copies prompt templates into `templates/`, seeds
   the glossary from any matching asset catalogues, generates a style
   profile from the opening chapters, and prepares a cover.
   Pass `--skip-profile` to skip the style-profile LLM call at init, and
   run `profile` later to (re)generate it.

3. Verify the endpoint answers for every job:

       uv run scripts/translate.py ping --project .

## Daily loop

    uv run scripts/translate.py status --project .
    uv run scripts/translate.py translate --next 3 --project .

`--next N` takes the next N pending chapters; `--chapters A-B` (or a spec
like `1,3-5,Chapter_0007.zh.md`) picks chapters explicitly. Chapters run
strictly in sequence on purpose: the glossary and note history build up as
you go. Ctrl-C is safe at any point -- per-chapter state is saved and a
rerun resumes where it stopped. Chapters in `needs-review` are skipped by
`--next`; they wait for `retry` or `mark`.

## Human review

- Read `translated/Chapter_NNNN.md` (one paragraph per line).
- Either hand-edit the file directly -- the epub builds from the file
  as-is -- or fix the cause (a wrong `glossary.json` term, a prompt
  template) and retranslate:

      uv run scripts/translate.py retry --chapters 7 --project .

- To see why a chapter is stuck:

      uv run scripts/translate.py status --why --project .

  prints the most recent reviewer feedback for every `needs-review`
  chapter.
- Fully manual chapters: write `translated/Chapter_NNNN.md` yourself
  following the format described in `references/file-formats.md`, then:

      uv run scripts/translate.py mark --chapters 7 --status translated --project .

## Glossary and notes upkeep between batches

`glossary.json` and `tn_history.json` are plain JSON -- edit them freely;
the next `translate` run picks the changes up. To re-run catalogue
seeding:

    uv run scripts/translate.py seed --project .

## Shipping

    uv run scripts/translate.py build-epub --project .

Validates with epubcheck via Docker (`--skip-check` to skip) and writes the
book into `export/`.

## Tuning (config.json)

- `providers` -- endpoint and model per job: `translator`, `glossary`,
  `reviewer`, `annotator`, `profile`.
- Temperature and `top_p` per provider. The translator defaults to
  temperature 0.7 and `top_p` 1.0 per the Hy-MT2 model card -- tune to
  taste.
- Thresholds: `min_term_coverage`, `tn_gap_chapters`, `max_attempts`,
  `translate_chunk_max_tokens`, `style_sample_chapters` / `style_sample_chars`,
  `contextual_glossary_cap`.

Every file schema (manifest, chapter state, glossary, notes, novel_info)
is documented in `references/file-formats.md`.
