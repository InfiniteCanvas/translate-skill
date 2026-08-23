# novel-translator

A staged, resumable novel-translation tool. It drives a self-hosted
OpenAI-compatible endpoint (sglang) through multiple passes per chapter --
line-indexed translation, glossary-consistency checks, a model faithfulness
gate, translator's notes -- while a project-local glossary keeps names and
terms consistent across the whole book. Finished chapters assemble into a
validated epub3.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) -- runs the CLI with its inline dependencies
- Docker with an `epubcheck` image available (EPUB validation; used by
  `build-epub`, including the automatic background rebuilds, or pass
  `--skip-check`)
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
   and `tn_history.json`, copies prompt templates into `templates/`,
   copies a style guide to `style.md`, seeds the glossary from any
   matching asset catalogues, and prepares a cover.

   Style is preset-based -- zero LLM calls at init. Pick with
   `--style <name|path>`: `classic` (default; standard xianxia/wuxia
   register), `transmigration` (modern protagonist voice + internet
   memes against a classic cultivation world), `modern` (contemporary
   settings), `literary` (elevated epic register), or a path to your own
   .md file. `style.md` is hand-editable (picked up on the next
   translate); drop .md files into the project's `styles/` to add or
   override presets. `styles` lists the presets (name + description):

       uv run scripts/translate.py styles --project .

   `--background "<2-4 sentences>"` records novel context for the
   translator's background frame. `--style auto` keeps the legacy
   model-generated profile (one LLM call over sampled chapters; the
   `profile` command regenerates it later).

3. Verify the endpoint answers for every job:

       uv run scripts/translate.py ping --project .

   `ping` tries `GET /models` first (with auth headers when the provider
   has `api_key`/`api_key_env`); hosted providers that don't serve that
   route fall back to a minimal chat completion, so an `[ok] ... (chat ok;
   /models failed: ...)` line still means the provider works.

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

  or retry every failed chapter at once:

      uv run scripts/translate.py retry --failed --project .

- To see why a chapter is stuck:

      uv run scripts/translate.py status --why --project .

  prints the most recent reviewer feedback for every `needs-review`
  chapter.
- Fully manual chapters: write `translated/Chapter_NNNN.md` yourself
  following the format described in `references/file-formats.md`, then:

      uv run scripts/translate.py mark --chapters 7 --status translated --project .

- Balance hard-failures trigger automatic pruning of mundane glossary
  terms (console: `[glossary] retired mundane term '...'`); a chapter
  whose failures were all mundane proceeds without a retry. The
  `glossary_cleanup` trace events in `logs/llm-YYYYMMDD.jsonl` show each
  removal (source + reason) and the kept terms; disable the pruning with
  `glossary_auto_cleanup: false`.

## Glossary and notes upkeep between batches

`glossary.json` and `tn_history.json` are plain JSON -- edit them freely;
the next `translate` run picks the changes up. To re-run catalogue
seeding:

    uv run scripts/translate.py seed --project .

## Shipping

    uv run scripts/translate.py build-epub --project .

Validates with epubcheck via Docker (`--skip-check` to skip) and writes the
book into `export/`. During `translate`/`retry` batches the epub also
refreshes automatically: a background build runs after every translated
chapter (serialized; triggers arriving mid-build coalesce) and a final
build at batch end guarantees the finished epub includes every chapter --
`export/` always holds a current, validated epub, so the manual command is
only needed for one-off builds. Auto-build failures are warnings only;
details land in `logs/epub-build.log`.

## Tuning (config.json)

- `providers` -- endpoint and model per job: `translator`, `glossary`,
  `reviewer`, `annotator`, `profile`.
- Temperature and `top_p` per provider. The translator defaults to
  temperature 0.7 and `top_p` 1.0 per the Hy-MT2 model card -- tune to
  taste.
- `thinking` per provider (default false) maps to sglang
  `chat_template_kwargs.enable_thinking`; set true per job for
  hybrid-thinking experiments. Symptom of thinking-on: ~minutes-long
  calls returning empty content.
- Hosted providers: any job can point at a 3rd-party OpenAI-compatible
  endpoint (e.g. put `reviewer` on GLM via `base_url`/`model`) with
  `api_key_env` (name of an environment variable holding the key --
  preferred) or `api_key` (inline) for `Authorization: Bearer` auth. The
  key never appears in trace logs. `base_url` handling: bare origins get
  `/v1` appended; a base with a real path (e.g.
  `https://api.z.ai/api/paas/v4`) is trusted exactly as written.
  `extra_body` on a provider block merges provider-specific parameters
  verbatim into the request body (after the known knobs, before
  `response_format`; not sent by ping's minimal probe).
- Thresholds: `min_term_coverage (advisory usage floor; only zero renderings hard-fail)`, `tn_gap_chapters`, `max_attempts`,
  `translate_max_output_tokens`, `style_sample_chapters` / `style_sample_chars`
  (only used by `--style auto`), `contextual_glossary_cap`.
- `tn_keep_low_confidence` (default false) — keep notes the annotator
  self-assessed as `threshold: "low"` instead of dropping them.
- `auto_build_epub` (default true) -- rebuild the epub in the background
  after every translated chapter during `translate`/`retry` (serialized,
  coalescing), with a guaranteed final build at batch end; set false to
  build only via `build-epub`.
- `glossary_auto_cleanup` (default true) -- when the balance gate
  hard-fails on a term, one glossary-model call judges it: mundane terms
  are retired from `glossary.json` (never re-added) instead of triggering
  retries; set false for strict retry-only behavior.

Every file schema (manifest, chapter state, glossary, notes, novel_info)
is documented in `references/file-formats.md`.
## Debugging

Every LLM call is TWO lines in `logs/llm-YYYYMMDD.jsonl`: an `llm_request`
line (params + full prompt, written before the call) and an `llm_response`
line (raw response, finish_reason, usage, timing), paired by `call_id`.
Pipeline attempts, balance advisories, and `glossary_cleanup` events are
interleaved in the same stream. The console is a summary, the log is truth. Disable the LLM lines
with `log_llm: false` in config.json.

Background epub builds append their output (including epubcheck results) to
`logs/epub-build.log`, with `=== epub build after Chapter_NNNN | timestamp ===`
separators between builds.

