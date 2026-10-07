# novel-translator

A staged, resumable novel-translation tool. It drives a self-hosted
OpenAI-compatible endpoint (sglang) through multiple passes per chapter --
line-indexed translation, glossary-consistency checks, a model faithfulness
gate, translator's notes -- while a project-local glossary keeps names and
terms consistent across the whole book and a rolling story-so-far recap
(one cheap extra call per chapter) keeps every prompt aware of the plot so
far. Finished chapters assemble into a validated epub3.

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

On Linux you can put it on your PATH instead (the script's shebang runs it
through uv automatically):

    ln -s <path-to>/novel-translator/scripts/translate.py ~/.local/bin/novel-translate
    novel-translate status --project <dir>

## One-time setup

1. Make a project directory and put source chapters in `source/` named
   `Chapter_NNN[a].md` (1-4 digit zero-padded number, optional single-letter
   suffix, e.g. `Chapter_001.md`, `Chapter_0002a.md`). Frontmatter is
   optional; `init` backfills novel-level fields and chapter titles.
   Starting from a website instead of files? `references/ingestion.md`
   walks through scraping a novel: TOC discovery, converting pages to
   chapter files, and the naming pitfalls. After init, newly downloaded
   chapters are picked up with `uv run scripts/translate.py sync`, which
   rebuilds `chapters.json` and preserves existing statuses.

2. Initialize the project:

       uv run scripts/translate.py init --project . \
           --title "<original title>" --author "<author>" \
           --source-url "<url>" --source-lang zh --target-lang en

   This writes `config.json`, `novel_info.json`, an empty `glossary.json`
   and `tn_history.json`, copies prompt templates into `templates/`,
   copies a style guide to `style.md`, seeds the glossary from any
   matching asset catalogues, and prepares a cover (scraped from the
   source URL or generated; `--cover-url URL` points at a cover image
   directly). `init` refuses to overwrite an existing `config.json`;
   `--force` reinitializes, resetting `glossary.json` and
   `tn_history.json` to empty and deleting `story_state.json` (the
   per-chapter `notes/` sidecars survive with their translated chapters).
   Only `--force` resets those three files: re-running `init` without it
   on a directory whose `config.json` was deleted preserves them and
   prints `[init] preserving existing glossary.json, tn_history.json, and
   story_state.json (pass --force to reset)`.
   Init also turns the project directory
   into a git repository (with a project-local git identity -- nothing
   global is touched), and every following action is committed with a
   descriptive subject, so `git log` is a labeled backup of the project.

   If you keep a `config.local.json` in the skill directory (optional, and
   gitignored so it never gets committed), `init` deep-merges it into the
   new `config.json` automatically -- your endpoint, model, and auth are
   applied once and reused by every project you start, instead of being
   retyped per novel. Keys the overlay does not mention (languages,
   thresholds, the `version` stamp) are left exactly as `init` wrote them.
   To apply it to a project that already exists:

       uv run scripts/translate.py sync-config --project .

   `sync-config` is the same merge, safe to re-run (a second run with no
   change reports `no changes`), and it prints the dotted key paths it
   changed. An absent overlay file is a clean no-op, not an error; a
   malformed one exits 2 with a `[FAIL]` line rather than leaving the
   project pointed at the wrong endpoint. Merge rules: an overlay
   `providers.<job>` given as an array replaces that job's blocks
   wholesale, while a single block object merges into the existing blocks
   (so `{"model": "x"}` keeps your `base_url` and auth). The overlay may
   not carry `version`. Note that an inline `"api_key"` is safe in the
   gitignored overlay but does get copied into the project's `config.json`,
   which the project repo commits -- both commands print a `[warn]` naming
   each one, and `"api_key_env": "MY_KEY"` avoids it entirely.

   After updating the skill itself, upgrade existing projects in place:

       uv run scripts/translate.py migrate --project .

   `migrate` is non-destructive and re-runnable: it brings a project up
   to the current skill version (new config keys, templates shipped
   since the project's init) and never touches `glossary.json`,
   `tn_history.json`, or `story_state.json` -- that reset is
   `init --force`'s job. Templates
   missing from the project are copied without asking; a copy that
   differs from the shipped one prompts, per template,
   `templates ~ <name>.md differs from the shipped copy - overwrite
   it? [y/N]` (Enter/n keeps the project's version, y overwrites;
   `--force` answers y to all, and non-interactive runs (piped,
   scripted, CI) keep differing templates with a `[warn]`; `--dry-run`
   reports without writing). An up-to-date project
   still gets this template maintenance pass (read-only when clean,
   version stamp untouched; a missing repository is backfilled), so
   `migrate --force` refreshes stale
   templates on current projects too. Each applied step is committed
   once it lands (`migrate: vNNN <description>`). The newest step, v009,
   raises the translation output budget (DESCRIPTION: `raise the
   translation output budget to 64k (translate_max_output_tokens
   8192->65536 and provider max_tokens 16384->65536, together)`):
   `translate_max_output_tokens` goes 8192 -> 65536 and every provider
   block still at 16384 goes to 65536 in the same step, because the
   truncation-retry cap is clamped to the smallest translator
   `max_tokens` -- raising only one would make the retry smaller than the
   attempt it retries. Values are rewritten only where they still equal the
   old default, so your own numbers are preserved. Reasoning-capable hosted
   models draw from this same budget (GLM-5.3 measured ~10.5k reasoning
   tokens on a translator's-notes pass), which is why the ceiling went up.
   Before it, v008 landed the provider-array batch (DESCRIPTION: `provider
   arrays + the consensus job (multi-model consensus); ship consensus.md`):
   every
   `providers.<job>` value is normalized to an array of provider blocks
   (a legacy single-block dict wraps into a one-element array; every
   user-set key kept verbatim) and the new `consensus` job is
   materialized (exactly one block), while the template sync ships the
   new `consensus.md` (the multi-model synthesis prompt); v007 landed
   the rolling-recap batch (DESCRIPTION: `materialize the recap
   provider job; ship recap.md and notes_review.md; refresh
   tn_generate.md`): the new `recap` provider block is folded into
   config.json (no new top-level key -- an omitted job inherits the
   `translator` block), the template sync ships the new `recap.md`
   (rolling story recap) and `notes_review.md` (`review notes` tier)
   prompts, and `tn_generate.md` is refreshed to the rewritten
   glossary-aware categorized version; v006
   backs the guide-only unit category (templates only -- no config
   change): `tn_generate.md` asks
   for a conversion note at a transliterated unit's first chapter
   occurrence, and `glossary_review.md` exempts `category: "unit"` entries
   from the mundane judgment; v005
   rewords the shipped glossary templates so glossary terms are restricted
   to named entities, named actions, and titles bound to a name
   (DESCRIPTION: `restrict glossary terms to named entities, named
   actions, and name-bound titles`; templates only -- no config change);
   v004 materializes the
   `min_term_occurrences` default (the novel-wide significance gate for
   glossary expansion); v003 gives existing
   projects the git repository -- it materializes the `git_commits`
   default and runs `git init` on the project, so even a repo's first
   commit captures the fully migrated state.

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
   has `api_key`/`api_key_env`); hosted providers with an explicit `model`
   configured that don't serve that route fall back to a minimal chat
   completion, so an `[ok] ... (chat ok; /models failed: ...)` line still
   means the provider works. One line per provider block: single-block
   jobs keep the bare padded job name (`[ok] translator <url> ->
   <model>`), while the blocks of a multi-block job are index-suffixed
   (`[ok] translator[0] <url> -> <model> (config model: <m>)`,
   `[ok] translator[1] ...`) and a repeated model inside one job warns
   (`[warn] translator[i]: same model as translator[j] (<model>) -
   candidates will be near-identical`).

## Daily loop

    uv run scripts/translate.py status --project .
    uv run scripts/translate.py translate --next 3 --project .

`--next N` takes the next N pending or in-progress chapters; `--chapters A-B` (or a spec
like `1,3-5,Chapter_0007.md`) picks chapters explicitly. Chapters run
strictly in sequence on purpose: the glossary, note history, and rolling
story recap build up as you go. Ctrl-C is safe at any point -- per-chapter
state is saved and a
rerun resumes where it stopped (mid-TRANSLATE down to the part: long
chapters split by per-line token-budget packing, and each validated part is
persisted, so a rerun continues at `[init] resuming translation at part
k/n`). Chapters in `needs-review` are skipped by
`--next`; they wait for `retry` or `mark`. Already-`translated` chapters
are skipped too; pass `--force` to retranslate them. Every finished
chapter lands as its own commit -- `translate: chapter NNNN (translated)`
or `translate: chapter NNNN (needs-review)` -- so the project's git
history grows one labeled entry per chapter. Each translated chapter also
refreshes a ≤ 120-word "story so far" recap in `story_state.json` (one
`recap`-provider call after assembly — a multi-model array fans it out
and merges via the consensus provider; the next chapter's prompts receive
it as context — advisory only, a recap failure never fails a chapter, and
retranslating a chapter refreshes only its own entry).

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

  prints the last few accumulated feedback entries (from any stage —
  translate, validate, or faith) for every `needs-review` chapter.
- Fully manual chapters: write `translated/Chapter_NNNN.md` yourself
  following the format described in `references/file-formats.md`, then:

      uv run scripts/translate.py mark --chapters 7 --status translated --project .

- Balance drift signals (advisory, never blocking) trigger automatic pruning
  of mundane glossary terms (console: `[glossary] retired mundane term
  '...' (<reason>)`, applied only once the chapter's translation is accepted); kept
  signals are surfaced to the faithfulness reviewer, which
  makes the final pass/fail call on terminology. The `glossary_cleanup`
  trace events in `logs/llm-*.jsonl` show each removal (source + reason)
  and the kept terms; disable the pruning with `glossary_auto_cleanup:
  false`.

## Glossary and notes upkeep between batches

`glossary.json` and `tn_history.json` are plain JSON -- edit them freely;
the next `translate` run picks the changes up. To re-run catalogue
seeding:

    uv run scripts/translate.py seed --project .

`--min-count N` overrides the seed threshold for the run;
`--catalogue PATH` (repeatable) seeds from explicit catalogue files,
bypassing the language filter. The commands in this section commit their
own action to the project's git history -- `seed: N glossary term(s)`,
`review: glossary audit` (suffixed `review: glossary audit (N fix(es)
applied)` when `--fix` landed fixes), `review: notes audit`, `glossary
replace` / `util replace`, and
`tn: re-check notes` (when at least one chapter changed -- or only
`tn_history.json` drifted, since kept notes bump `times`/`last_order`
even when every sidecar is identical).

To find entries before editing them (read-only; see Bulk review fixes for
the matching semantics):

    uv run scripts/translate.py glossary search --project . TERM \
        [--max-distance N]

To sanity-check a term before hand-adding it -- or to see why expansion
skipped a proposal -- count its occurrences across the source chapters
(read-only):

    uv run scripts/translate.py glossary count --project . TERM \
        [--variants "A,B"] [--chapters SPEC] [--min N]

    [glossary] count '裴小丫' (+1 variant(s)) across 42 chapter(s)
    [glossary] Chapter_0003.md: 2
    [glossary] total: 7 occurrence(s) in 2/42 chapter(s)
    [ok] '裴小丫' meets the significance threshold (min 3)

Automatic glossary expansion is gated the same way: a proposed brand-new
term is only added when it occurs at least `min_term_occurrences`
(default 3) times across the whole novel -- and the gate covers new
variants too: a re-proposal of an existing entry is not re-gated on its
source, but each NEW variant it carries must clear the same floor, and a
below-threshold variant is dropped. Never gated: real merges, and
already-present variants being restated. Below the threshold the command
prints
`[warn] '<term>' is below the significance threshold: <total> < <min>`
and exits 1.

To audit entry quality (nothing else does -- the balance check only
counts occurrences, cleanup only judges drift-flagged terms):

    uv run scripts/translate.py review glossary --project .

Two tiers: deterministic heuristics (duplicate/variant collisions, a
translation shared by several entries, translation still in the source
language or equal to the source, a missing category (info -- legal for
minimal hand-added entries, other stages treat it as "other"), unknown
category, non-CJK text in a CJK entry's variants) plus the glossary
model judging alignment,
definitions, categories, cross-entry conflicts, and mundane entries --
class nouns like 麦穗 "wheat stalks", standalone titles, and kinship or
address terms ("great grandmother") that never belonged in the glossary
(seeded/catalogue and `category: "unit"` entries exempt) -- in batches of 40 by default
(`review_batch_size`; `--batch-size N` overrides for the run).
Report-only by default -- one
`[glossary] warn|info '<source>' -> '<translation>': <kind> - <reason>`
line per finding plus a summary (console: `[glossary] review: N entries
(B model batch(es) of up to S)` before the model calls, `[glossary]
reviewing batch i/n` per batch, `[glossary] warn batch i/n review
failed - <error>` after a failed batch, then the post-run `[glossary]
review: N entries, W warn / I info findings` with outstanding counts);
`--fix` opts in to guarded fixes
(model-suggested fixes only: direct model-tier warn findings, or a
suggestion the merge borrowed onto a heuristic finding;
translation/definition/category only; validated; conflicting and
already-applied suggestions skipped (re-runs are safe); prints
`[glossary] fixed ...` per change). Mundane findings carry
no suggestion -- `--fix` never retires them; their `- Command:` bullet
does. Exit 0 clean or info-only, 1 warns remain, 2 usage error (review
flags are per subject: `review glossary` accepts `--fix` and
`--batch-size`, `review notes` accepts `--chapters` and `--batch-size`,
`review fix` accepts `--glossary` / `--dry-run` / `--stale-ok` /
`--exit-on-error`;
anything else exits 2 with
`[FAIL] --<flag> does not apply to 'review <subject>'`; `--fix` on
`review fix`/`review notes` keeps its own message,
`[FAIL] --fix applies to 'review glossary' only; not 'review <subject>'`). Cost
ceil(N/review_batch_size) model calls (each batch fans out to every
model when the job's provider array is multi-block).

Every run also writes `<project>/review-report.md` (filename from the
`review_report_path` config key; overwritten each run,
clean runs included; console: `[glossary] report: <path>`): a YAML
frontmatter block with the run's counts (entries reviewed, batch errors,
glossary digest, outstanding warn/info, machine-applicable vs manual-review tallies, and
the `[N]` indices of the manual-review findings), then the numbered
outstanding findings in two sections -- `## Machine-applicable` (apply
with `review fix`) first, then `## Needs manual review` (decide yourself
or hand to an agent); warn before info, source-ascending within a
severity -- each finding with its reason, suggestion, tier, the full
glossary entry as JSON, an Action line (model-written when available,
else a per-kind template), and, in the machine-applicable section only, a
`- Command:` bullet on every finding whose fix is fully determined by its
structured fields -- a mundane entry's bullet is `glossary retire --source
S`, which deletes the entry and appends its source to glossary.json's
top-level `retired` list so seeding and glossary expansion never re-add
it. Findings fixed by `--fix` drop out of the numbering into
a "Fixed automatically" section; guarded-out suggestions sit under "Fixes
skipped (need a decision)", and a footer walks the next steps (hand-edit
glossary.json, `review fix --glossary review-report.md` for the offline
batch path, re-run to confirm exit 0, `retry --chapters N` for chapters
already translated with a wrong rendering). The report is meant for
delegating fixes by index:

    fix items 1, 4, and 5 in review-report.md doing what was suggested

For the offline machine-actionable path, run
`uv run scripts/translate.py review fix --glossary review-report.md
[--dry-run] [--stale-ok] [--exit-on-error]`: it runs every `- Command:` bullet in the
`Machine-applicable` section as a subprocess (`glossary replace | set |
merge | retire`), in order, and exits 0 on full success or full no-op, 1
if any command failed (continues past failures by default;
`--exit-on-error` to stop at the first), 2 on a missing/unreadable report
or a report with no machine-applicable commands. `review fix` accepts
exactly `--glossary` (the report path, not a glossary selector --
`review glossary --glossary X` is a usage error), `--dry-run`,
`--stale-ok`, and
`--exit-on-error`; any other review flag exits 2 with
`[FAIL] --<flag> does not apply to 'review <subject>'` (`--fix` keeps
its own message: `[FAIL] --fix applies to 'review glossary' only; not
'review <subject>'`). A stale report -- one whose `glossary_digest`
frontmatter no longer matches the live glossary.json -- is refused with
exit 1: `[review fix] report is stale (glossary changed since
generation) - regenerate with review glossary`; `--stale-ok` skips the
check. Legacy reports (the
pre-split format: no frontmatter, severity-grouped findings, no
`- Command:` bullets, old `- Command:` header that records the generating
command) are synthesized on the fly from the structured parts alone -- no
`review glossary` re-run needed.

When a glossary translation changes (hand edit or `review glossary --fix`),
chapters already translated still carry the old rendering. Rewrite it in
place -- no retranslation needed:

    uv run scripts/translate.py glossary replace --project . --source 灵根 \
        --translation "spiritual root" [--keep-alt] [--no-build] [--dry-run]
    uv run scripts/translate.py util replace --project . \
        --source "spirit root" --target "spiritual root" [--dry-run]

`glossary replace` finds the entry by source or variants (exit 2 unknown),
rewrites the old rendering across chapters first, and saves the translation
to glossary.json last (a no-op, exit 0, when it already equals the new
one). That ordering makes a mid-rewrite failure recoverable: glossary.json
still says the old translation, the console prints `[warn] replace
incomplete: k/N chapters rewritten; glossary.json not updated - re-run the
same command to finish`, and the command fails cleanly (exit 2); re-running
the same command completes it (already-rewritten chapters match zero
occurrences and are skipped, so the re-run is idempotent). `util replace`
is the raw phrase -> replacement variant for arbitrary term fixes. Both are
offline (no LLM calls) and match smartly: case-insensitive, words joined
by spaces or hyphens, optional es/s/ed/ing inflection on the last word
(capitalization preserved, inflection re-appended -- Spirit Root ->
Spiritual Root), CJK as literal substrings, `[^N]` markers untouched. Only
the chapter body is rewritten (frontmatter byte-verbatim, only files with
matches, atomic LF; chapters come from the manifest). By default the old
rendering is pruned from the entry's `alt_translations` (a stale alt would
mask balance drift); `--keep-alt` keeps it. `--dry-run` prints the glossary
diff and per-chapter counts. Exit 0 success, 2 usage error (no manifest,
unknown term) or an interrupted rewrite (glossary.json untouched -- re-run
the same command to finish). The epub rebuilds once after changed chapters when
`auto_build_epub` is on (default); pass `--no-build` to skip that rebuild
(use when running many replaces from `review fix`, which always passes it
itself and runs exactly one final epub build at the end). Build failures
are warnings only.

Translator's notes have their own re-evaluation path: they live in
`notes/<stem>.json` sidecars next to the chapters, never in the chapter
markdown, so re-running the annotator can't disturb chapter prose. Point it
at a chapter range:

    uv run scripts/translate.py tn --project . --chapters 1-5 [--dry-run] [--no-build]

`tn` re-runs the annotator over the range (translated chapters only;
untranslated ones are skipped with a warning) and regenerates each
`notes/<stem>.json` from scratch through the same prompt and dedup as
the pipeline (glossary-aware categorized annotation, low-threshold gate,
within-chapter dedup, cross-chapter gap
rule vs `tn_history.json` — a term annotated within `tn_gap_chapters` in an
earlier chapter stays suppressed — and the same code-enforced
`max_notes_per_chapter` cap: the prompt asks for severity-ordered entries
and the cap truncates after dedup, so the most severe context loss
survives). Discarded candidates — low-threshold, cap overflow, invalid —
are recorded in `notes/<stem>.dropped.json` next to the sidecar (a
review artifact; the epub builder does not read it); the pipeline prints
the same record as `[Chapter_NNNN] [ok] notes: K kept (cats); D dropped
(reasons) -> notes/<stem>.dropped.json`. An annotator response of zero
notes for a chapter that has notes is treated as a failed evaluation:
the sidecar and translated markdown are left untouched
(`[tn] <file>: annotator returned 0 notes for a chapter with N note(s) -
keeping existing sidecar`). Chapter prose is never rewritten,
with
one exception: chapters from before the sidecar migration (notes baked into
the markdown as `[^N]` markers + a Translator's Notes section) are cleaned
once, on their first re-evaluation. Afterwards the epub rebuilds
automatically unless `--no-build`. `--dry-run` still makes the annotator
LLM calls but writes nothing (not even the legacy cleanup). Exit 0 success,
1 failed chapters (annotator call or unreadable chapter) or no eligible
chapters in range, 2 usage error.

To audit the notes that already exist (no re-annotation, nothing
rewritten) — flagging notes that fail to earn their place, since the goal
is notes that add context or explain context lost in translation:

    uv run scripts/translate.py review notes --project . \
        [--chapters SPEC] [--batch-size N]

`review notes` audits every chapter with a `notes/<stem>.json` sidecar
in manifest order (`--chapters SPEC` restricts; chapters without sidecars
are skipped silently). Each note's stored line index is re-resolved with
the epub builder's anchor rules; a note whose anchor matches no translated
line becomes a `misanchored` warn deterministically, without a model
call. Every resolvable note is judged in `review_batch_size` batches
(default 40; `--batch-size N` overrides) by the `reviewer` provider
through `templates/notes_review.md`, paired with its translated line, the
line-aligned source line, and the ±2-line target context — a
multi-model `reviewer` array fans each batch out to every model in
parallel and merges via the consensus provider. Judgment kinds:
`restates` (adds nothing the translation doesn't already say),
`overexplains` (common knowledge or inferable from context — fails the
comprehension threshold), `wrong` (misexplains the source term),
`misanchored` (rides the wrong line). Advisory only: exit 0 regardless of
finding count, no `--fix` (passing one is a usage error, exit 2, as are
`--batch-size` < 1, a bad `--chapters` spec, and any other flag that
does not apply to the subject --
`[FAIL] --<flag> does not apply to 'review <subject>'`), and the report it writes (same
`review_report_path` file as `review glossary`, same per-run overwrite —
a notes run replaces a previous glossary report) carries no
`- Command:` bullets; fixes are hand edits to `notes/<stem>.json`
(delete the note, reword it, or re-attach it to the right line). The
report footer carries the caveat: hand-edited sidecars are overwritten if
the `tn` re-check command later regenerates that chapter's notes. Console:
`[notes] reviewing batch i/n`, then `[ok] review notes: N findings
(restates X, overexplains Y, wrong Z, misanchored W) -> <report path>`,
plus a `[warn]` hint when N > 0 that fixes are hand edits to
`notes/<stem>.json`; a project with no sidecars prints `[ok] no chapter
notes found - nothing to review` (genuinely no sidecars -- when chapters
existed but all were skipped, `[warn] no chapter notes reviewed: N
chapter(s) skipped (see failures above)` prints instead). Commits
`review: notes audit`.

## Bulk review fixes

`review glossary` may leave dozens of machine-determinable findings
behind (every `mistranslation` / `wrong_language` / `collision` /
`definition` / `category` finding with a suggestion, plus heuristic
duplicate / variant collisions with structured merge data and mundane
terms to retire). Applying them one at a time by hand or by an agent is
tedious and prone to drift. For the offline path:

    uv run scripts/translate.py review fix --glossary review-report.md \
        [--dry-run] [--stale-ok] [--exit-on-error]

`review fix` parses every `- Command:` bullet in the report's
`Machine-applicable` section and runs each as a subprocess (`glossary
replace | set | merge | retire`), in order; it commits nothing itself --
each spawned glossary subcommand commits its own action.
Commands are pre-validated: a
`glossary replace` / `set --translation` whose suggested value contains
source-script characters
for a CJK-source entry is skipped in-process (`[review fix] skipped [N]:
suggestion not in target language`) and counted as needing a decision --
the same guard `review glossary --fix` enforces. So is a hand-written
`glossary set` command whose `--category` value is outside the category
vocabulary (`[review fix] skipped [N]: unknown category '<value>' (must
be one of: place, person, org, skill, technique, level, state, item,
honorific, unit, other) (<command line>)`). So is a Command bullet
carrying `--project` in either form (`[review fix] skipped [N]: command
overrides --project`) -- the executor always prepends its own `--project`,
and one smuggled into a hand-edited report would silently retarget the
command. Conflicts are keyed per field, not per verb: `replace` shares
`set --translation`'s key (both rewrite the translation), a multi-field
`set` claims every field it writes, and a command naming a variant
collides with one naming the canonical source -- the first queued wins,
and a later command conflicting on any claimed key is skipped
(`[review fix] skipped [N]: conflicting command for '<source>' (already
queued)`). Exit codes: 0 on full
success or full no-op, 1 if any command failed (continues past failures
by default; `--exit-on-error` to stop at the first) or the report was
refused as stale (`--stale-ok` overrides), 2 on a
missing/unreadable report or a report with no machine-applicable
commands. `--dry-run` prints each command with its
finding index — annotating `SKIP` on commands the real run would skip
(an invalid suggestion, an unknown `--category`, a smuggled `--project`,
or a conflict with an earlier command) — plus a one-line summary (`N
command(s), M would be skipped (invalid or conflicting), K finding(s)
need a decision`, skip count only when nonzero) and applies nothing. The `- Command:` bullet is the contract: `write_report()` emits
it on every finding whose fix is fully determined by its structured
fields; the closed vocabulary is documented in
`references/file-formats.md` (per-finding `glossary replace` for
mistranslation / wrong_language / collision with a suggestion; `glossary
set --definition` / `--category` for definition / category findings with
a suggestion; `glossary set --remove-variant` for heuristic variants;
`glossary merge --keep ... --remove ...` for heuristic duplicates;
`glossary retire --source S` for mundane findings).
Delete any `- Command:` bullet to veto that finding; legacy reports in the
pre-split format (no frontmatter, findings grouped by severity, without
`- Command:` lines, and the old `- Command:` header that records the
generating command) are synthesized on the fly from the structured parts
alone -- no `review glossary` re-run needed. After applying, one
final `build-epub` runs when chapters changed and `auto_build_epub` is on;
the `- Command:` lines in the report never carry `--no-build`, so they
remain human-copyable.

The batch-flow subcommands:

    uv run scripts/translate.py glossary set --project . --source S \
        [--translation T] [--definition D] [--category C]
        [--add-variant V] [--remove-variant V]
        [--alt-translations "A,B"] [--add-alt A] [--remove-alt A]
    uv run scripts/translate.py glossary merge --project . --keep K --remove R
    uv run scripts/translate.py glossary retire --project . --source X
    uv run scripts/translate.py glossary search --project . TERM \
        [--max-distance N]
    uv run scripts/translate.py glossary count --project . TERM \
        [--variants "A,B"] [--chapters SPEC] [--min N]

`glossary set` applies any combination of `--translation`, `--definition`,
`--category`, `--add-variant` / `--remove-variant`,
`--alt-translations` / `--add-alt` / `--remove-alt` atomically -- one save,
all-or-nothing -- and is idempotent (exit 0 when nothing actually
changed; the changeless run prints `[glossary] noop: <detail>`);
`--category` is whitelisted against the same list `apply_fixes`
uses, `--translation` is CJK-checked against the source like `apply_fixes`
(definitions may legitimately quote CJK terms and are stored as-is).
Assigning `--category unit` to an entry with a non-empty translation
warns `[warn] glossary: '<source>' has a translation but category 'unit'
(guide-only: balance checks skip it)`.
`--alt-translations "A,B"` REPLACES the existing list;
`--add-alt` / `--remove-alt` edit it in place.

`glossary merge --keep K --remove R` transfers `variants` /
`alt_translations` / `definition` from R to K (definition only fills K
when K's is empty; a definition arriving for a K that already has one is
discarded with `[warn] glossary: definition from '<removed>' discarded
('<kept>' already has one)`), appends R to the top-level `retired` list, and
removes R from `terms` -- idem-potent (re-running with an already-retired
R, canonical source or variant spelling, is a clean no-op exit 0; the
supplied spelling is recorded in `retired` alongside the canonical
source). The kept entry's `translation` / `category` / `origin` /
`first_seen_chapter` are preserved.

`glossary retire --source X` is a thin wrapper over `glossary.retire()`
for a single source; X matches by source or variants, and the entry's
canonical source is what gets recorded in `retired`. Re-running with
the entry's canonical source prints `[glossary] noop: already retired: X`
and exits 0; re-running with a variant spelling of an already-retired entry
is also a clean no-op (exit 0) -- the supplied spelling is recorded in
`retired` alongside the canonical source.

`glossary replace` accepts `--no-build` to skip the post-success epub
build for batch callers (the replace behavior itself is unchanged).

`glossary search TERM` is the read-only lookup that pairs with the editing
subcommands: it matches TERM case-insensitively against every entry's
`source`, `variants`, `translation`, and `alt_translations` (both sides) --
substring containment always hits, and Levenshtein within `--max-distance`
(default: config `fuzzy_max_distance`; `0` = substring only) matches whole
values or single words with
no separator special-casing (`grand elder` finds `grand-elder`). Retired
matches print `[glossary] retired match: ...` info lines. Exit 0 with
matches, 1 none, 2 error.

`glossary count TERM` is the read-only significance check: it counts
non-overlapping occurrences of TERM plus any `--variants` (comma-separated)
across the source chapters -- matched longest-first, so a nickname inside a
full name counts once -- and prints a per-chapter breakdown.
`--chapters SPEC` restricts the count with the same SPEC semantics as
`translate --chapters`; `--min N` overrides the threshold (default: the
`min_term_occurrences` config key). Exit 0 at or above the threshold, 1
below it (`[warn] '<term>' is below the significance threshold: <total> <
<min>`), 2 usage error.

## Shipping

    uv run scripts/translate.py build-epub --project .

Validates with epubcheck via Docker (`--skip-check` to skip) and writes the
book into `export/`. During `translate`/`retry` batches the epub also
refreshes automatically: a background build runs after every translated
chapter (serialized; triggers arriving mid-build coalesce) and a final
build at batch end guarantees the finished epub includes every chapter --
`export/` always holds a current epub (epubcheck-validated when Docker is
available), so the manual command is
only needed for one-off builds. Auto-build failures are warnings only --
a stalled build is killed after 360s (the kill takes the builder's whole
process tree, which reaches the docker CLI and its children, but the
daemon-side epubcheck container is not the builder's child and may run
to completion -- docker's `--rm` reaps it; a kill that itself fails or a
builder that survives it warns `[warn] epub auto-build: failed to kill
builder (taskkill: <error>) - it may still be running` / `[warn] epub
auto-build builder survived the kill - it may still be running` instead
of raising, and a confirmed kill sweeps the dead build's leftover
`export/*.epub.<pid>.tmp` with `[warn] removed N stale epub temp file(s)
left by the killed build`), a failed child prints
`[warn] epub auto-build failed, exit <code> (after <reason>) - see
logs/epub-build.log`, a Ctrl-C interrupt prints
`[warn] epub auto-build interrupted`, and with epubcheck unavailable the
rebuild is skipped with `[warn] epub auto-build could not run (epubcheck
unavailable) - skipping validation`, and a finalize that waits out its
budget prints `[warn] epub auto-build finalize waited <n>s for the builder
to exit`; details land in
`logs/epub-build.log`.

## Tuning (config.json)

- `providers` -- endpoint and model per job: `translator`, `glossary`,
  `reviewer`, `annotator`, `recap`, `profile`, `consensus`. The `recap` job
  generates the rolling story-so-far recap (one cheap call per translated
  chapter; a small model is a good fit). Each job's value is an array of
  provider blocks (a bare block object is the legacy single-model shape
  and loads unchanged); two or more blocks run multi-model consensus --
  every prompt fans out to all the job's models in parallel and one
  `consensus`-provider call merges the candidates into the final
  response under the task's own JSON schema. The `consensus` job itself
  is exactly one block (omitted, it defaults to the translator's first
  block — that block's settings win, with temperature 0.2 filling what it
  leaves unset); any other omitted job inherits the
  translator's whole array, each element onto the job's own defaults.
  Two translator models:

      "translator": [
        { "base_url": "http://100.85.218.125:8888/v1", "model": null,
          "temperature": 0.7, "top_p": 1.0, "max_tokens": 65536, "thinking": false },
        { "base_url": "http://100.85.218.125:8889/v1", "model": "Qwen3-235B-A22B",
          "temperature": 0.7, "top_p": 1.0, "max_tokens": 65536, "thinking": false }
      ]
- Temperature and `top_p` per provider. The translator defaults to
  temperature 0.7 and `top_p` 1.0 per the Hy-MT2 model card -- tune to
  taste.
- `thinking` per provider (default false) maps to sglang
  `chat_template_kwargs.enable_thinking`; set true per job for
  hybrid-thinking experiments. Symptom of thinking-on: ~minutes-long
  calls returning empty content. This flag is **sglang-only and is ignored
  by hosted providers** — a hosted reasoning model (e.g. MiniMax
  `M3.1-Flash-Preview`) thinks at full depth regardless and can spend an
  entire 64k budget without emitting a single character. Bound it through
  `extra_body` instead: `reasoning_effort` (default is `max`, which is
  non-terminating on a full chapter), or `thinking: {type: "disabled"}`
  for models that accept it. See `config.local.EXAMPLES.md`.
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
- Don't want to re-enter the same settings for every new novel? Keep them
  in `config.local.json` beside the skill's `scripts/` directory (gitignored,
  any subset of `config.json`'s keys). `init` merges it into each new
  project and `sync-config --project .` applies it to an existing one; see
  the setup section above and `references/file-formats.md`.
- Thresholds: `min_term_coverage` (advisory usage floor; a term with >= 2
  source occurrences and zero renderings becomes a drift signal for the
  FAITH reviewer), `tn_gap_chapters`, `max_attempts`,
  `translate_max_output_tokens` (per-call output cap and the packing
  budget: the whole chapter goes in one call while its expected output
  fits `floor(0.8 × translate_max_output_tokens) − 256`, and longer
  chapters split into parts that each fit that budget; a
  truncating part retries once at ~1.5x, capped by the smallest
  `max_tokens` across the translator's blocks (the minimum governs
  packing and retries so no model in the array truncates its part); a
  single line too big for even that fails fast with
  feedback to split or shorten it), `style_sample_chapters` / `style_sample_chars`
  (only used by `--style auto`), `contextual_glossary_cap`.
- `tn_keep_low_confidence` (default false) — keep notes the annotator
  self-assessed as `threshold: "low"` instead of dropping them.
- `auto_build_epub` (default true) -- rebuild the epub in the background
  after every translated chapter during `translate`/`retry` (serialized,
  coalescing), with a guaranteed final build at batch end; set false to
  build only via `build-epub`.
- `glossary_auto_cleanup` (default true) -- when balance drift signals
  are flagged (advisory), one glossary-model call judges each term:
  mundane terms are retired from `glossary.json` (never re-added;
  deferred until the translation passes the faithfulness gate) while
  kept signals go to the faithfulness reviewer; set false to disable the
  retirement.
- `git_commits` (default true) -- commit every mutating action to the
  project's git repo (created by `init`, backfilled by migrate v003); set
  false to keep a project un-versioned.
- `fuzzy_max_distance` (default 2) -- Levenshtein tolerance for word
  matches in the balance check; also the default for
  `glossary search --max-distance`.
- `max_new_terms_per_chapter` (default 15) -- cap on new glossary terms
  proposed per chapter.
- `min_term_occurrences` (default 3) -- minimum novel-wide occurrences
  for glossary expansion to add a brand-new term (0 disables the gate);
  also the default threshold for `glossary count`.
- `max_notes_per_chapter` (default 10) -- cap on translator's notes
  generated per chapter, enforced in code after dedup (the annotator
  returns severity-ordered entries, so the cut tail is the least severe
  context loss; discards land in `notes/<stem>.dropped.json`).
- `review_batch_size` (default 40) -- entries per `review glossary` /
  `review notes` model review call; `--batch-size N` overrides it for one
  run.
- `review_report_path` (default `review-report.md`) -- filename of the
  advisory review report, relative to the project dir; written by
  `review glossary` / `review notes` (whichever ran last owns the file),
  read back by `review fix`.

Every file schema (manifest, chapter state, glossary, notes, novel_info)
is documented in `references/file-formats.md`.
## Debugging

Every LLM call logs an `llm_request` line and an `llm_response` line in the
run log — `logs/llm-<timestamp>-<command>-<pid>.jsonl`, one file per project per CLI
invocation: the request (params + full prompt, written before the call) and
the response (raw response, finish_reason, usage, timing), paired by
`call_id`; a call that hits the 400 fallback (retry without
`response_format`) adds one extra `llm_request` line for the retried
request. A multi-model job's fan-out logs one request/response pair per
candidate (meta `{"job": <job>, "candidate": i, "candidates": n}`, i
1-based) plus the consensus call (meta `{"job": "consensus",
"consensus_for": <job>}`), so a job with N models in its array makes
N + 1 calls per task where the single-model pipeline makes one.
Pipeline attempts, `balance_advisory` events (which now carry drift
signals alongside under-use warnings and over-count info),
`glossary_cleanup` events, `glossary_review` events (entries, batches,
batch_errors, findings, applied, skipped), `notes_review` events
(chapters, units, batches, batch_errors, skipped, findings -- one per
`review notes` run), and `review_fix` events
(specs_run, applied, noop, failed, skipped_invalid, skipped_conflict,
changed_chapters, needs_decision -- one per `review fix` run) are
interleaved in the same
stream. The console is a
summary, the log is truth.
Each run prunes older logs to the newest `log_llm_keep_runs` (default 5);
disable the LLM lines with `log_llm: false` in config.json.

Background epub builds append their output (including epubcheck results) to
`logs/epub-build.log`, with `=== epub build after Chapter_NNNN.md | timestamp ===`
separators between builds.

Because every mutating action is committed (see the `git_commits` config
key), `git log`, `git show`, and `git checkout <sha> -- <file>` work as
project history and undo -- each commit subject names the action that
produced it.

