# File Formats — novel-translator

Schemas and contracts for every file in a translation project. The scripts in
`scripts/lib/` are the source of truth for behavior; this file is the source of
truth for *shapes* the user (or the agent) is expected to read, edit, or
hand-fix.

## Project layout

```
<project>/
├── config.json          settings: languages, LLM providers, thresholds
├── novel_info.json      book metadata (title, author, source, tags)
├── style.md             active style guide (copied from a preset at init, hand-editable)
├── chapters.json        manifest: every chapter, its order and status
├── glossary.json        translation glossary (seeded + model-grown)
├── tn_history.json      translation-note history (powers the 10-chapter rule)
├── review-report.md     indexed `review glossary` findings (regenerated per run)
├── source/              Chapter_NNNN[a].md — untouched source chapters
├── draft/               working area per chapter (see Draft artifacts)
├── translated/          finalized chapters, exactly what the epub is built from
├── notes/               per-chapter translator's-note sidecars (notes/<stem>.json)
├── covers/cover.jpg     scraped or generated cover
├── templates/           per-project copies of the prompt templates (editable)
├── styles/              optional per-project style presets (add/override .md files)
├── export/              built epubs
└── logs/                llm-*-<command>-<pid>.jsonl (one LLM trace per CLI invocation, newest log_llm_keep_runs kept); epub-build.log (background epub-build output)
```

Chapter file names must match `Chapter_NNNN.md` (1-4 digit zero-padded
number, matching is case-insensitive), optionally with a letter suffix for
extras/bonus chapters:
`Chapter_0042a.md` sorts between `Chapter_0042.md` and `Chapter_0043.md`.
Chapter order = position in the file list sorted numerically by the parsed
`(number, suffix)` — so `Chapter_999` sorts before `Chapter_1000`; that order
is written into each source file's frontmatter and into `chapters.json` as
`order` (0-based), and ALL chapter-distance logic (the translation-note gap)
measures distance in `order` units.

## Source chapter format

Markdown with YAML frontmatter:

```yaml
---
source_url: https://example.com/novel/chapter-1
novel_title: 凡人修仙传
chapter_title: 第二章 山边小村
author: 忘语
order: 1            # managed by the tool; do not edit by hand
---

Body text, one paragraph per line. Blank lines are preserved as line 0-indices.
```

Only `order` is managed by the tool; the rest is metadata carried through to
the translated copy. The body is split on `\n` and translated as an indexed
JSON array — one source line in, one translated line out, always the same
count. This is the anti-hallucination backbone of the whole pipeline.

## config.json

```jsonc
{
  "source_lang": "zh",
  "target_lang": "en",
  "providers": {
    "translator": { "base_url": "http://100.85.218.125:8888/v1", "model": null,
                    "temperature": 0.7, "top_p": 1.0, "max_tokens": 16384,
                    "thinking": false },   // sglang chat_template_kwargs.enable_thinking; false = output budget spent on the answer, not a reasoning chain
    "glossary":    { "...same shape, temperature 0.2" },
    "reviewer":    { "...same shape, temperature 0.0",
                     // hosted-provider example: any job can call a 3rd-party
                     // OpenAI-compatible endpoint with Bearer auth
                     // "base_url": "https://api.z.ai/api/paas/v4", "model": "glm-5.3",
                     // "api_key_env": "ZAI_API_KEY" },   // or "api_key": "sk-..." inline
                     // optional "extra_body": { "thinking": { "type": "disabled" } }
                     // merges provider-specific params verbatim into the request body
                     // (after the known knobs, before response_format; ping's probe never sends it)
                   },
    "annotator":   { "...same shape, temperature 0.2" },
    "profile":     { "...same shape, temperature 0.3" }   // style-profile generation (--style auto / `profile` only)
  },
  "seed_min_count": 3,           // catalogue term must appear >= N times in source/ to seed
  "min_term_coverage": 0.25,     // ADVISORY usage floor: below ceil(coverage*src) warns; 0 renderings with src>=2 is a drift signal handed to the FAITH reviewer
  "fuzzy_max_distance": 2,       // Levenshtein tolerance for single-word targets of >= 5 letters; multi-word phrases match case-insensitively with hyphen/space equivalence plus an optional inflection on the final word
  "glossary_auto_cleanup": true, // balance drift signals: retire mundane terms via a cleanup judgment; kept signals go to the FAITH reviewer; false = skip the judgment
  "tn_gap_chapters": 10,         // re-annotate a term only after > N chapters of distance
  "tn_keep_low_confidence": false, // keep threshold:"low" notes instead of dropping them (default drops)
  "auto_build_epub": true,      // rebuild the epub in the background after every translated chapter (serialized; final build at batch end); false = manual `build-epub` only
  "max_attempts": 3,             // translation attempts before needs-review
  "contextual_glossary_cap": 200, // safety valve only — every glossary term present in the chapter goes in
  "max_new_terms_per_chapter": 15,
  "max_notes_per_chapter": 10,
  "translate_max_output_tokens": 8192, // per-call OUTPUT cap (card recommends 4k-8k) + chunk threshold: expected output above this splits the chapter; input context is never limited
  "style_sample_chapters": 4,    // chapters sampled (at random) for style-profile generation (--style auto only)
  "style_sample_chars": 12000,   // rough source-character budget for the sample (--style auto only)
  "log_llm": true,               // full request/response LLM trace; false disables the LLM trace lines only
  "log_llm_keep_runs": 5,        // one llm-*.jsonl per CLI invocation; older logs pruned to the newest N (by mtime)
  "version": 1                   // project version (see Migrations) — written by `init` (fresh projects are born current) and `migrate` (stamped after each successfully applied step) ONLY, never merged from DEFAULTS — the raw on-disk value is the source of truth; a config.json without the key is version 0
}
```

- `base_url` includes `/v1` (OpenAI-compatible). `model: null` means "ask
  `/models` and use the first model" — resolved at runtime, works with any
  sglang/vLLM server.
- Each job can point at a **different provider**: keep `translator` on the
  translation model, later point `reviewer` at a stronger model. Any job block
  you omit inherits the `translator` block.
- **Sampling knobs**: `temperature` and `top_p` (plus optional `top_k`,
  `repetition_penalty`) are per-provider and passed through to the server.
  Translator defaults follow the Hy-MT2 model card (0.7 / 1.0) — tune to taste
  per project; translation quality at different temperatures is subjective.
- Language codes (`zh`, `en`, ...) are mapped to full names ("Chinese",
  "English") automatically at prompt-build time, per the model card's
  guidance. Templates always see full names.
- No API keys are stored; if the endpoint needs one, add `"api_key": "..."`
  (sent as `Authorization: Bearer`).

## novel_info.json

```jsonc
{
  "title": "凡人修仙传",
  "title_translated": "A Record of a Mortal's Journey to Immortality",  // optional; epub title falls back to title
  "author": "忘语",
  "source_url": "https://example.com/novel",
  "tags": ["xianxia", "cultivation"],
  "source_lang": "zh",
  "target_lang": "en",
  "created_at": "2026-08-23T12:00:00",
  "cover": "covers/cover.jpg",
  "style": "transmigration",                  // name of the chosen style preset (recorded at init)
  "background": "A former MBA student wakes up in the body of a doomed sect outer disciple...",  // optional; fed to the [Background Information] frame
  "style_profile": {                          // LEGACY: only written by `--style auto` init / `profile`
    "style_summary": "Warm, understated wuxia prose with dry humor...",
    "background": "A slow-burn cultivation novel told in close third person..."
  }
}
```

`title_translated` is worth filling by hand — it becomes the epub title, the
export filename (`export/<title-slug>.epub`; without it, a CJK title is
preserved verbatim in the filename and the console suggests setting it), and
the primary text on a generated cover. **Style** resolution: the project
`style.md` (copied from the chosen preset at init, hand-editable — edits
apply on the next translate, no re-init) → legacy
`style_profile.style_summary` → a generic default descriptor. **Background**
resolution: `novel_info.background` → legacy `style_profile.background` →
empty.

## chapters.json (manifest)

```jsonc
[
  { "file": "Chapter_0001.md", "number": 1, "suffix": "", "order": 0,
    "status": "translated", "title": "山边小村" },
  { "file": "Chapter_0002.md", "number": 2, "suffix": "", "order": 1,
    "status": "pending", "title": "青牛镇" }
]
```

Statuses: `pending` → `in-progress` → `translated` | `needs-review`.
`needs-review` chapters are skipped by `translate --next` on purpose — they
need a human/agent decision (see SKILL.md), then `retry` or `mark`.

## glossary.json

```jsonc
{
  "terms": [
    {
      "source": "筑基",
      "translation": "Foundation Establishment",
      "variants": ["築基"],              // alternative source-script forms AND nicknames/short forms rendered identically (e.g. 小丫 for 裴小丫); counted longest-first so overlaps don't double-count
      "alt_translations": ["Foundation Establishment stage"],
      "definition": "Second realm of cultivation; the cultivator's body is rebuilt.",
      "category": "level",               // place|person|org|skill|technique|level|state|item|honorific|other
      "origin": "seeded",                // seeded (catalogue) | model (proposed during translation)
      "first_seen_chapter": 12           // order index where a model-proposed term first appeared
    }
  ],
  "retired": ["灵气"]                    // optional; sources removed from the glossary (balance auto-cleanup, glossary merge/retire) — always the entry's CANONICAL source, even when the term was matched via a variant; seed and glossary expansion skip them, delete a source here to allow re-adding
}
```

- The balance check counts `source`+`variants` occurrences in the source text
  and `translation`+`alt_translations` occurrences in the translated text
  (stemmed, case-insensitive word/phrase matching with Levenshtein tolerance 2
  for words ≥ 5 letters; exact substring match for CJK targets), cross-entry
  longest-first — a term nested inside a longer glossary compound (仙界
  inside 修仙界) is credited to the longer term only. The check is FULLY
  ADVISORY: no condition fails a chapter. Falling below the usage floor
  `ceil(min_term_coverage × src)` (default 25%) is a console warning, and
  exceeding `src + max(2, src)` is logged only. Drift signals — the canonical
  rendering appears ZERO times while the term occurs `src >= 2` times — first
  run the cleanup judgment (one `glossary`-provider call,
  `templates/glossary_cleanup.md`; disable with
  `glossary_auto_cleanup: false`): mundane terms are removed into `retired`
  — deferred until the translation passes the FAITH gate, so a rejected
  attempt retires nothing —
  and kept signals are appended to the FAITH reviewer's prompt, which owns
  the pass/fail verdict — it fails genuine drift but passes legitimate
  counting false positives (nested compounds, inflections/hyphenations,
  generic words). All three tiers land in the trace log as
  `balance_advisory` events (`drift_signals` / `warnings` / `over_count`
  arrays) for human review, and cleanup results land as `glossary_cleanup`
  events — fields `chapter`, `removed: [{source, reason}]`,
  `kept: [sources]`. A `review glossary` run logs one `glossary_review`
  event — fields `entries`, `batches`, `batch_errors`, `findings` (each
`{source, kind, severity, reason, suggestion, action, origin, fixable}` —
`action` is a model-written fix instruction (empty when the suggestion
suffices); `fixable: true` only on heuristic findings whose suggestion was
merge-borrowed from the model; heuristic findings also carry **optional**
`variant_to_remove` (variant finding) and `merge_with` (duplicate finding)
keys whose value is the structured string the writer needs to emit a
machine-applicable Command, so downstream consumers never have to parse
free-form reason text. The `--fix`
  results `applied: [{source, field, kind, old, new}]` /
  `skipped: [{source, field, reason}]`.
- Hand-editing entries between runs is safe and encouraged — the file is read
  fresh before every chapter. Hand-added entries need at least `source` and
  `translation`. `glossary replace` (which sets `translation` and rewrites
  the old rendering in translated chapters) prunes that rendering from the
  entry's `alt_translations` by default — a stale alt would keep the balance
  check counting the old rendering as valid, masking drift; `--keep-alt`
  leaves alt_translations untouched.

## review-report.md

Regenerated by every `review glossary` run at `<project>/review-report.md`
(overwritten — a clean run writes a report that says so; console:
`[glossary] report: <path>`). It is the indexed, agent-actionable view of
the findings the `glossary_review` trace event records: run the review,
then either tell an agent "fix items 1,4,5 in review-report.md doing what
was suggested" or run `review fix --glossary review-report.md` to apply
every machine-determinable finding offline in one command. **Delete any
`- Command:` bullet to veto that finding** — `review fix` only runs what
the report lists.

````markdown
# Glossary Review Report

- Generated: 2026-08-24T12:00:00+00:00       # UTC timestamp
- Generated by: `review glossary`           # ... `--fix` when it ran with it
                                              # (the report's per-finding
                                              # `- Command:` bullets are
                                              # the fix contract; this header
                                              # bullet is the provenance)
- Languages: Chinese -> English
- Entries reviewed: 214 (6 model batch(es))   # + ", N batch error(s) — findings
                                              #   from failed batches are missing"
- Outcome: 3 warn / 2 info outstanding        # + ", N fixed automatically"

## Warnings (fix before translating further)

### [1] warn / mistranslation / 筑基

- Reason: <why the entry was flagged>
- Suggestion: Foundation Establishment      # only when one exists
- Tier: model                               # heuristic | model
- Entry:                                    # the FULL glossary entry, pretty JSON,
                                             # so no glossary.json lookup is needed
  ```json
  { "source": "筑基", "translation": "Base Building", "variants": [],
    "definition": "...", "category": "level", "origin": "seeded" }
  ```

- Action: Set the `translation` field of this entry to "Foundation Establishment".
- Command: glossary replace --source '筑基' --translation 'Foundation Establishment'
                                             # omitted when the fix lives in
                                             # free-form Action prose only
                                             # (model-tier duplicate/variant,
                                             # suggestion-less collision,
                                             # `other`)

## Info (optional improvements)             # numbering continues: [4], [5], ...

## Fixed automatically (--fix)              # --fix results that left the numbering:
                                             #   `source`: field 'old' -> 'new'
## Fixes skipped (need a decision)          # guarded-out suggestions:
                                             #   `source` (field): reason

## Next steps                               # footer: hand-edit glossary.json (safe,
                                             # re-read every chapter); `review fix
                                             # --glossary review-report.md` to apply
                                             # every machine-determinable finding;
                                             # re-run `review glossary` to confirm
                                             # exit 0; `retry --chapters N` for
                                             # chapters already translated with a
                                             # wrong rendering
````

Indices `[1]`, `[2]`, ... number OUTSTANDING findings only — warnings first,
then info, continuous across the two sections; findings resolved by `--fix`
this run are excluded from the numbering and listed under "Fixed
automatically" instead. Each **Action** line is the model-written
action when provided, else a deterministic per-kind template:
mistranslation / wrong_language / definition / category → set that field to
the suggestion (or decide the correct value); duplicate → merge with the
other owner of the string, keep one entry, delete the other, and add the
removed source to the top-level `"retired"` list; variant → remove the
flagged string from `variants` (or move it to `alt_translations` if it is
really an alternative translation); collision → give the entry a
`translation` distinct from the other entry's, or move the shared rendering
to `alt_translations`; mundane → retire the term — delete the entry and add
its source to the top-level `"retired"` list so seeding and glossary
expansion never re-add it.

The `- Command:` bullet — when present — is the contract `review fix`
honors. Every value is `shlex.quote()`-escaped, so CJK source terms and
spaces survive copy-pasting. The `- Command:` bullet is emitted only
when the fix is **fully determined by the finding's structured fields**
(see `command_for_finding()` in `lib/review.py`); the closed mapping is:

| Finding kind + data                                  | Emitted command |
|---|---|
| `mistranslation` / `wrong_language` with suggestion  | `glossary replace --source S --translation T` |
| `collision` with suggestion                          | `glossary replace --source S --translation T` |
| `definition` with suggestion                         | `glossary set --source S --definition D` |
| `category` with suggestion                           | `glossary set --source S --category C` |
| heuristic `variant` (has `variant_to_remove`)        | `glossary set --source S --remove-variant V` |
| heuristic `duplicate` (has `merge_with`)             | `glossary merge --keep M --remove S` |
| `mundane`                                            | `glossary retire --source S` |
| model-tier `duplicate` / `variant`, suggestion-less `collision`, `other` | _(no Command bullet — decision item)_ |

`review fix` pre-validates each command before running it: a `glossary
replace` / `set --translation` whose suggested value still contains
source-script characters for a CJK-source entry is skipped in-process
(console: `[review fix] skipped [N]: suggestion not in target language`)
and counted as needing a decision — the same guard `review glossary
--fix` applies to its own fixes.

Older reports (no `- Command:` bullets, `- Command:` header that
records the generating command) remain fully supported: `review fix`
synthesises the same commands from the heading + `- Suggestion:` bullet
+ the two exact heuristic reason templates, so an old report needs no
review re-run.

Header bullet history: the `- Generated by:` form above applies to
reports written by current and future versions. Legacy reports keep
the older `- Command: review glossary` header; `review fix` ignores it
because its argv (`review glossary`) does not start with a supported
glossary verb.

## Migrations (`scripts/migrations/` in the skill)

Per-version upgrade steps for the `migrate` subcommand. One module per
version, named `v001.py`, `v002.py`, ... — zero-padded so sort order =
version order; the chain is discovered from the files present, and the
skill's current version is the highest one. Each module exposes exactly:

| Attribute | Meaning |
|---|---|
| `VERSION` | int; must equal the number in the module's filename |
| `DESCRIPTION` | one-line summary of what the step does |
| `migrate(project_dir, templates_src, dry_run, force, confirm=None) -> list[str]` | applies the step; returns the report lines to print |

`confirm` is an optional `Callable[[str], bool]` supplied by
`cmd_migrate` for interactive runs (`None` = non-interactive); steps
never touch stdin directly, they just call `confirm(...)` and honor
the boolean it returns.

`migrate` reads the project's `config.json` `version` (a config without
the key is version 0), runs only the steps with a higher version in
order, and stamps `version` after each successfully applied step —
immediate per-step stamping, so a crash mid-chain resumes at the failed
step. `v001` — the only step so far — materializes merged config
defaults onto disk (keys introduced after the project's init, e.g.
`glossary_auto_cleanup` and `min_term_coverage`, plus provider-job
normalization; user-set values always preserved) and copies shipped
templates missing from the project's `templates/` dir without prompting
(non-destructive; the runtime fallback would cover them anyway). A
template that exists but differs from the shipped one is asked about
interactively, one prompt per template — `overwrite templates/<name>.md
with the shipped version? [y/N]`: Enter/n keeps the project's version
(the default; it may be user-customized), y overwrites it with the
shipped copy. `--force` answers yes to all prompts (no prompting), and
non-interactive runs (stdin not a TTY: piped, scripted, CI) never
prompt and never block — differing templates are kept with a `[warn]`
line (`--dry-run` reports differing templates without prompting or
writing). A project already current is not a bare no-op: `migrate` runs
a read-only-when-clean template maintenance pass (same prompt rules)
that leaves the version stamp untouched — the chain gates structural
steps, and template refresh is maintenance, not a chain step. Adding a
new migration is nothing more than shipping the next `v<NNN>.py` — a
skill update that needs project-side changes ships that module and
nothing else.

## tn_history.json

```jsonc
{
  "筑基": { "note": "Foundation Establishment (筑基) is the second realm...", "last_order": 42, "times": 3 },
  "灵石": { "note": "Spirit stones are both currency and cultivation fuel.", "last_order": 7, "times": 1 }
}
```

Keyed by the source term. A note is attached to a chapter only if the term was
never annotated, or the last annotation is more than `tn_gap_chapters` order
positions away — and even within the gap, only an annotation from an
earlier, different chapter suppresses. A note recorded in the SAME chapter
(retranslate/retry) is restored, and retranslating an earlier chapter after
a later one annotated the term re-annotates it (the reader hits the earlier
chapter first). `last_order`/`times` are managed by the tool. Notes the model
self-assessed as `threshold: "low"` are dropped before all of this unless
`tn_keep_low_confidence` is true.

## notes/<stem>.json (translator's-note sidecar)

```jsonc
{
  "chapter": "Chapter_0042.md",
  "updated_at": "2026-09-06T12:00:00+00:00",
  "notes": [
    { "line": 17, "term": "筑基",
      "note": "Foundation Establishment (筑基) is the second realm of cultivation.",
      "anchor": "His breakthrough settled at dawn, and the whole courtyard" }
  ]
}
```

One JSON sidecar per translated chapter (`notes/Chapter_0042.json` for
`translated/Chapter_0042.md`), holding the translator's notes the epub
renders as footnotes — the chapter markdown itself stays clean. Written by
the pipeline's ASSEMBLE stage and by the `tn` command; read per chapter by
`build-epub`, which falls back to parsing legacy baked-in `[^N]` markers
when the sidecar is absent (chapters translated before the sidecar
existed). Each note is exactly `{line, term, note, anchor}`: `line` is a
0-based index into the translated body lines as normalized by
`read_chapter` (leading/trailing blank lines stripped), and `anchor`
snapshots the first 80 characters of that line so the epub build can
re-resolve the note onto the right paragraph after hand-edits shift line
numbers — the stored index wins when its line still starts with the
anchor, else the first line starting with the anchor wins, else the note
is dropped with a warning. Entries are validated on save (`line` in range,
non-empty string `term`/`note`); a save that keeps zero notes DELETES the
sidecar (absent = no notes). `updated_at` is managed by the tool.

## Draft artifacts (`draft/`)

For `Chapter_0001.md` the pipeline creates:

| File | Contents |
|---|---|
| `Chapter_0001.md` | human-readable current translation draft (frontmatter + lines) |
| `Chapter_0001.lines.json` | `{"title": "...", "lines": [...]}` — written per attempt as a debug artifact; nothing reads it back |
| `Chapter_0001.state.json` | pipeline state: `{"stage", "attempt", "title", "lines", "feedback": [...], "notes": [...], "updated_at", "pipeline"}` — `pipeline` is the state-schema version; `title`/`lines` hold the draft translation (crash-resume past TRANSLATE) |

`stage` is one of `TRANSLATE, VALIDATE, BALANCE, FAITH, GLOSSARY_EXPAND,
TN_GENERATE, TN_DEDUP, ASSEMBLE`. `feedback` accumulates everything the gates
rejected (faithfulness reasons, including genuine term drift flagged by
balance signals) and is re-injected into
every retry prompt. The state file is deleted after a chapter is assembled
into `translated/`.

## Translated chapter format (epub-builder contract)

What `assemble` writes and `build-epub` parses — keep this shape when
hand-fixing a chapter:

```markdown
---
source_url: https://...
novel_title: 凡人修仙传
chapter_title: 第二章 山边小村
title: Chapter 2: A Small Village by the Mountains   # translated title (tool-added)
author: 忘语
order: 1
---

One translated paragraph per line, same count as the source.
```

- Clean markdown only: no footnote markers, no Translator's Notes section.
  Translator's notes live in the per-chapter sidecar (`notes/<stem>.json`,
  previous section); the epub builder renders them as inline epub3 footnotes
  (`<a epub:type="noteref">` → `<aside epub:type="footnote">`). Chapters
  from older projects that still bake `[^N]` markers + a
  `## Translator's Notes` section into the markdown keep building — the
  builder falls back to parsing them out of the file — and the `tn`
  command migrates them (rewrites the markdown clean, notes move to the
  sidecar) on their next re-evaluation.
- One translated line per source body line. Exception: when the source body
  opened with a line identical to `chapter_title`, the pipeline strips it and
  carries the translated title in the frontmatter `title` field only — such
  chapters have one fewer body line than their source file.
- TOC label = `title` (falls back to `chapter_title`, then the file stem).
- `util replace` / `glossary replace` rewrite the body (everything after the
  frontmatter, a legacy baked-in Translator's Notes section included) in
  place when a rendering changes: frontmatter stays byte-verbatim, only
  files with matches are rewritten, atomically, LF.

During `translate`/`retry`, `build-epub` also runs automatically after every
chapter reaches `translated`: per-chapter rebuilds are serialized (with a
guaranteed final build at batch end) and failures are warnings only (output
in `logs/epub-build.log`), so `export/` always holds a current epub;
disable with `auto_build_epub: false`.

## Prompt templates (`templates/`)

Copied from the skill's `assets/templates/` at `init`; edit freely per project.
A template file missing from the project's `templates/` dir falls back to
the skill's `assets/templates/`, so newly shipped templates work in
existing projects. `migrate` materializes missing templates onto disk
and prompts for each copy that differs from the shipped one —
`overwrite templates/<name>.md with the shipped version? [y/N]`
(Enter/n keeps the project's copy, y overwrites; `--force` answers y
to all prompts, and non-interactive runs keep differing copies with a
`[warn]`); this template maintenance pass runs on already-current
projects too, leaving the version stamp untouched. Plain
`{{placeholder}}` substitution. The pipeline
errors out if a template still contains an unknown/leftover `{{...}}`
after filling — typos fail fast.

| Template | Filled for | Placeholders |
|---|---|---|
| `translation.md` | TRANSLATE (per chunk) | `target_lang glossary feedback_section chapter_title chunk_info style background_section source_lines line_count` |
| `glossary_expand.md` | GLOSSARY_EXPAND | `source_lang target_lang glossary source_lines translation_lines max_terms` |
| `faithfulness.md` | FAITH | `source_lang target_lang source_lines translation_lines background_section balance_signals_section` |
| `tn_generate.md` | TN_GENERATE | `source_lang target_lang source_lines translation_lines background_section max_notes` |
| `glossary_merge.md` | glossary collision merge | `existing_json proposed_json` |
| `glossary_cleanup.md` | balance drift-signal cleanup | `source_lang target_lang term_list sample_lines` |
| `glossary_review.md` | `review glossary` model tier | `source_lang target_lang entries` |
| `style_profile.md` | `--style auto` init / `profile` (legacy) | `source_lang target_lang sample_text` |

`source_lines` / `translation_lines` are substituted as JSON arrays (compact,
`ensure_ascii=False`); `glossary_review.md`'s `entries` as one compact JSON
object per line (one batch of entries per model call). The `glossary` block
renders in the Hy-MT2 trained terminology format — one pure pair per line:
`筑基 translates to "Foundation Establishment"` — with no categories or
definitions in the translation prompt (they live in glossary.json for the
other stages).
`source_lang`/`target_lang` are always full language names. `style` is the
active style guide (project `style.md`, else legacy
`style_profile.style_summary`, else a generic default); `background_section`
renders the trained `[Background Information]` frame (`novel_info.background`,
else legacy `style_profile.background`, else empty, plus the previous chunk's
final lines for chunks 2+; the section is empty when no background is set and
the chunk has no predecessor).

**Whole-chapter translation**: only the OUTPUT is constrained. Each translate
call sends `max_tokens = translate_max_output_tokens` (default 8192, the model
card's recommended range); chapters whose EXPECTED output fits that cap are
translated in ONE call — the model sees the chapter's full context (input is
never limited by this). Longer chapters split into balanced parts sized so
each part's expected output fits the cap, with style background and the
previous part's final lines included as input context. `chunk_info` is empty
for single-call chapters.

## Model response schemas

Requested with sglang guided JSON (`response_format`) when available, with
robust extraction as fallback:

- TRANSLATE → `{"title": str, "lines": [{"i": int, "t": str}, ...]}` — the
  numbered-line protocol: input lines arrive as `{"i", "t"}` objects (1-based
  chapter-global index) and each translated line echoes its input `i`.
  Coverage is verified exactly (missing/duplicate/out-of-range indices become
  corrective feedback). Chapters whose body opens with a line identical to the
  frontmatter `chapter_title` have that line stripped during per-attempt
  preparation (before TRANSLATE) — the title is carried by the frontmatter
  `title` field instead.
- GLOSSARY_EXPAND → `{"terms": [{"source", "variants": [str], "translation", "definition", "category"}]}` —
  a proposal whose source is contained in a known term's source, or contains
  it, with the same translation (nicknames/short forms) is absorbed as a
  variant of the known entry, never a separate entry
- FAITH → `{"verdict": "SUCCESS"|"FAILURE", "reasons": [str]}`
- TN_GENERATE → `{"notes": [{"line": int, "term": str, "note": str, "threshold": "high"|"low"}]}` —
  the threshold is the model's self-assessed comprehension judgment
  (Hy-MT2's cultural-adaptation pattern); entries marked `"low"` are discarded
  automatically, missing threshold keeps the note
- style profile → `{"style_summary": str, "background": str}` (--style auto
  only; stored in novel_info.json)
- glossary merge → single entry `{"source", "translation", "definition", "category"}`

## Catalogues (`assets/catalogues/` in the skill)

```jsonc
{ "language": "zh", "name": "...",
  "terms": [ { "source": "练气", "variants": ["練氣"], "translation": "Qi Condensation",
               "alt_translations": ["Qi Refining"],
               "category": "level", "definition": "First major realm of cultivation." } ] }
```

`alt_translations` is optional but recommended for terms with more than one
accepted rendering — the balance check counts `translation` +
`alt_translations` in the translated text, so listing the alternatives
prevents false drift signals.

Catalogues are split by domain, not just language: `zh` currently ships three
(`zh-cultivation.json`, `zh-wuxia.json`, `zh-modern.json`). A catalogue for a
new language (ja/ko) or a new domain is just another JSON file with the right
`language` — `init`/`seed` pick up every catalogue matching `source_lang`.
