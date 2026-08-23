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
├── source/              Chapter_NNNN[a].md — untouched source chapters
├── draft/               working area per chapter (see Draft artifacts)
├── translated/          finalized chapters, exactly what the epub is built from
├── covers/cover.jpg     scraped or generated cover
├── templates/           per-project copies of the prompt templates (editable)
├── styles/              optional per-project style presets (add/override .md files)
├── export/              built epubs
└── logs/                llm-YYYYMMDD.jsonl (LLM trace); epub-build.log (background epub-build output)
```

Chapter file names must match `Chapter_NNNN.md` (4-digit zero-padded number),
optionally with a lowercase letter suffix for extras/bonus chapters:
`Chapter_0042a.md` sorts between `Chapter_0042.md` and `Chapter_0043.md`.
Chapter order = position in the lexicographically sorted file list; that order
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
                   },
    "annotator":   { "...same shape, temperature 0.2" },
    "profile":     { "...same shape, temperature 0.3" }   // style-profile generation (--style auto / `profile` only)
  },
  "seed_min_count": 3,           // catalogue term must appear >= N times in source/ to seed
  "min_term_coverage": 0.25,     // ADVISORY usage floor: below ceil(coverage*src) warns; only 0 renderings with src>=2 hard-fails
  "fuzzy_max_distance": 2,       // Levenshtein tolerance when matching translated glossary terms
  "glossary_auto_cleanup": true, // balance hard-failures: retire mundane terms via a cleanup judgment instead of retrying on them; false = strict retry-only
  "tn_gap_chapters": 10,         // re-annotate a term only after > N chapters of distance
  "tn_keep_low_confidence": false, // keep threshold:"low" notes instead of dropping them (default drops)
  "auto_build_epub": true,      // rebuild the epub in the background after every translated chapter (serialized; final build at batch end); false = manual `build-epub` only
  "max_attempts": 3,             // translation attempts before needs-review
  "contextual_glossary_cap": 200, // safety valve only — every glossary term present in the chapter goes in
  "max_new_terms_per_chapter": 15,
  "max_notes_per_chapter": 10,
  "translate_max_output_tokens": 8192, // per-call OUTPUT cap (card recommends 4k-8k) + chunk threshold: expected output above this splits the chapter; input context is never limited
  "style_sample_chapters": 4,    // chapters sampled (at random) for style-profile generation (--style auto only)
  "style_sample_chars": 12000    // rough source-character budget for the sample (--style auto only)
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

`title_translated` is worth filling by hand — it becomes the epub title and the
primary text on a generated cover. **Style** resolution: the project
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
  "retired": ["灵气"]                    // optional; sources removed by balance auto-cleanup — seed and glossary expansion skip them, delete a source here to allow re-adding
}
```

- The balance check counts `source`+`variants` occurrences in the source text
  and `translation`+`alt_translations` occurrences in the translated text
  (stemmed, case-insensitive word/phrase matching with Levenshtein tolerance 2
  for words ≥ 5 letters; exact substring match for CJK targets). The gate is
  MOSTLY ADVISORY — the count is a heuristic poisoned by shared renderings
  ("Senior" inside "Senior Brother"), generic English words, and
  noun-frequency mismatch between the languages. Only one condition
  hard-fails: the canonical rendering appears ZERO
  times while the term occurs `src >= 2` times — the reliable drift/omission
  signal. The failure path runs the cleanup judgment before retrying (one
  `glossary`-provider call, `templates/glossary_cleanup.md`; disable with
  `glossary_auto_cleanup: false`): mundane terms are removed into `retired`,
  and a chapter whose failures are all cleaned proceeds WITHOUT a retry —
  the translation was fine; the gate tripped on a term that shouldn't have
  been enforced. Kept failures retry (toward needs-review) as before.
  Falling below the usage floor `ceil(min_term_coverage × src)`
  (default 25%) is a console warning, and exceeding `src + max(2, src)` is
  logged only; both land in the trace log as `balance_advisory` events
  (`warnings` / `over_count` arrays) for human review, and cleanup results
  land as `glossary_cleanup` events — fields `chapter`,
  `removed: [{source, reason}]`, `kept: [sources]`.
- Hand-editing entries between runs is safe and encouraged — the file is read
  fresh before every chapter. Hand-added entries need at least `source` and
  `translation`.

## tn_history.json

```jsonc
{
  "筑基": { "note": "Foundation Establishment (筑基) is the second realm...", "last_order": 42, "times": 3 },
  "灵石": { "note": "Spirit stones are both currency and cultivation fuel.", "last_order": 7, "times": 1 }
}
```

Keyed by the source term. A note is attached to a chapter only if the term was
never annotated, or the last annotation is more than `tn_gap_chapters` order
positions away. `last_order`/`times` are managed by the tool. Notes the model
self-assessed as `threshold: "low"` are dropped before all of this unless
`tn_keep_low_confidence` is true.

## Draft artifacts (`draft/`)

For `Chapter_0001.md` the pipeline creates:

| File | Contents |
|---|---|
| `Chapter_0001.md` | human-readable current translation draft (frontmatter + lines) |
| `Chapter_0001.lines.json` | `{"title": "...", "lines": [...]}` — the machine array |
| `Chapter_0001.state.json` | pipeline state: `{"stage", "attempt", "feedback": [...], "notes": [...], "updated_at"}` |

`stage` is one of `PREP, TRANSLATE, VALIDATE, BALANCE, GLOSSARY_EXPAND, FAITH,
TN_GENERATE, TN_DEDUP, ASSEMBLE`. `feedback` accumulates everything the gates
rejected (balance violations, faithfulness reasons) and is re-injected into
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

One translated paragraph per line, same count as the source.[^1]

## Translator's Notes

[^1]: **筑基** — Foundation Establishment is the second realm of cultivation.
```

- Footnote markers `[^N]` sit at the END of the line (paragraph) they annotate;
  the epub builder turns them into inline epub3 footnotes
  (`<a epub:type="noteref">` → `<aside epub:type="footnote">`).
- One translated line per source body line. Exception: when the source body
  opened with a line identical to `chapter_title`, the pipeline strips it and
  carries the translated title in the frontmatter `title` field only — such
  chapters have one fewer body line than their source file.
- The `## Translator's Notes` section is always last if present.
- TOC label = `title` (falls back to `chapter_title`).

During `translate`/`retry`, `build-epub` also runs automatically after every
chapter reaches `translated`: per-chapter rebuilds are serialized (with a
guaranteed final build at batch end) and failures are warnings only (output
in `logs/epub-build.log`), so `export/` always holds a current epub;
disable with `auto_build_epub: false`.

## Prompt templates (`templates/`)

Copied from the skill's `assets/templates/` at `init`; edit freely per project.
A template file missing from the project's `templates/` dir falls back to
the skill's `assets/templates/`, so newly shipped templates work in
existing projects. Plain `{{placeholder}}` substitution. The pipeline
errors out if a template still contains an unknown/leftover `{{...}}`
after filling — typos fail fast.

| Template | Filled for | Placeholders |
|---|---|---|
| `translation.md` | TRANSLATE (per chunk) | `source_lang target_lang glossary feedback_section chapter_title chunk_info style background_section source_lines line_count` |
| `glossary_expand.md` | GLOSSARY_EXPAND | `source_lang target_lang glossary source_lines translation_lines max_terms` |
| `faithfulness.md` | FAITH | `source_lang target_lang source_lines translation_lines background_section` |
| `tn_generate.md` | TN_GENERATE | `source_lang target_lang source_lines translation_lines background_section max_notes` |
| `glossary_merge.md` | glossary collision merge | `existing_json proposed_json` |
| `glossary_cleanup.md` | balance-failure cleanup | `source_lang target_lang term_list sample_lines` |
| `style_profile.md` | `--style auto` init / `profile` (legacy) | `source_lang target_lang sample_text` |

`source_lines` / `translation_lines` are substituted as JSON arrays (compact,
`ensure_ascii=False`). The `glossary` block renders in the Hy-MT2 trained
terminology format — one pure pair per line: `筑基 translates to
"Foundation Establishment"` — with no categories or definitions in the
translation prompt (they live in glossary.json for the other stages).
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
  frontmatter `chapter_title` have that line stripped at PREP — the title is
  carried by the frontmatter `title` field instead.
- GLOSSARY_EXPAND → `{"terms": [{"source", "variants": [str], "translation", "definition", "category"}]}` —
  proposals whose source is contained in a known term with the same
  translation (nicknames/short forms) are absorbed as variants, never
  separate entries
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
accepted rendering — the balance gate counts `translation` +
`alt_translations` in the translated text, so listing the alternatives
prevents false drift failures.

Catalogues are split by domain, not just language: `zh` currently ships three
(`zh-cultivation.json`, `zh-wuxia.json`, `zh-modern.json`). A catalogue for a
new language (ja/ko) or a new domain is just another JSON file with the right
`language` — `init`/`seed` pick up every catalogue matching `source_lang`.
