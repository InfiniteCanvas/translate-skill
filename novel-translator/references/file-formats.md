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
├── chapters.json        manifest: every chapter, its order and status
├── glossary.json        translation glossary (seeded + model-grown)
├── tn_history.json      translation-note history (powers the 10-chapter rule)
├── source/              Chapter_NNNN[a].md — untouched source chapters
├── draft/               working area per chapter (see Draft artifacts)
├── translated/          finalized chapters, exactly what the epub is built from
├── covers/cover.jpg     scraped or generated cover
├── templates/           per-project copies of the prompt templates (editable)
└── export/              built epubs
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
                    "temperature": 0.7, "top_p": 1.0, "max_tokens": 16384 },
    "glossary":    { "...same shape, temperature 0.2" },
    "reviewer":    { "...same shape, temperature 0.0" },
    "annotator":   { "...same shape, temperature 0.2" },
    "profile":     { "...same shape, temperature 0.3" }   // style-profile generation
  },
  "seed_min_count": 3,           // catalogue term must appear >= N times in source/ to seed
  "balance_tolerance": 0.2,      // missing glossary hits may exceed max(1, tol*src); extras get max(2, src)
  "fuzzy_max_distance": 2,       // Levenshtein tolerance when matching translated glossary terms
  "tn_gap_chapters": 10,         // re-annotate a term only after > N chapters of distance
  "max_attempts": 3,             // translation attempts before needs-review
  "contextual_glossary_cap": 200, // safety valve only — every glossary term present in the chapter goes in
  "max_new_terms_per_chapter": 15,
  "max_notes_per_chapter": 10,
  "translate_chunk_size": 40,    // chapters longer than this are translated in balanced chunks
  "style_sample_chapters": 4,    // chapters sampled (at random) for style-profile generation
  "style_sample_chars": 12000    // rough source-character budget for the sample
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
  "style_profile": {                          // generated by init / `profile`, hand-editable
    "style_summary": "Warm, understated wuxia prose with dry humor...",
    "background": "A slow-burn cultivation novel told in close third person..."
  }
}
```

`title_translated` is worth filling by hand — it becomes the epub title and the
primary text on a generated cover. `style_profile` feeds every translation
prompt (summary into the style rule, background into the
`[Background Information]` frame); regenerate with `profile`, edit by hand
freely. Projects without one fall back to a generic style descriptor.

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
  ]
}
```

- The balance check counts `source`+`variants` occurrences in the source text
  and `translation`+`alt_translations` occurrences in the translated text
  (stemmed, case-insensitive word/phrase matching with Levenshtein tolerance 2
  for words ≥ 5 letters; exact substring match for CJK targets). The gate is
  asymmetric: missing occurrences are only allowed `max(1, balance_tolerance ×
  src)` slack, extra occurrences get `max(2, src)` slack (English often needs
  the glossary noun where the source uses a compound).
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
positions away. `last_order`/`times` are managed by the tool.

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

## Prompt templates (`templates/`)

Copied from the skill's `assets/templates/` at `init`; edit freely per project.
Plain `{{placeholder}}` substitution. The pipeline errors out if a template
still contains an unknown/leftover `{{...}}` after filling — typos fail fast.

| Template | Filled for | Placeholders |
|---|---|---|
| `translation.md` | TRANSLATE (per chunk) | `source_lang target_lang glossary feedback_section chapter_title chunk_info style background_section source_lines line_count` |
| `glossary_expand.md` | GLOSSARY_EXPAND | `source_lang target_lang glossary source_lines translation_lines max_terms` |
| `faithfulness.md` | FAITH | `source_lang target_lang source_lines translation_lines background_section` |
| `tn_generate.md` | TN_GENERATE | `source_lang target_lang source_lines translation_lines background_section max_notes` |
| `glossary_merge.md` | glossary collision merge | `existing_json proposed_json` |
| `style_profile.md` | init / `profile` | `source_lang target_lang sample_text` |

`source_lines` / `translation_lines` are substituted as JSON arrays (compact,
`ensure_ascii=False`). The `glossary` block renders in the Hy-MT2 trained
terminology format — one pure pair per line: `筑基 translates to
"Foundation Establishment"` — with no categories or definitions in the
translation prompt (they live in glossary.json for the other stages).
`source_lang`/`target_lang` are always full language names. `style` is the
style-profile summary; `background_section` renders the trained
`[Background Information]` frame (novel background always, plus the previous
chunk's final lines for chunks 2+; empty for unprofiled projects).

**Chunked translation**: chapters longer than `translate_chunk_size` lines
(default 40) are split into balanced chunks; each chunk is translated with its
own exact line-count requirement and one corrective retry, then concatenated.
The chapter title comes from the first chunk. `chunk_info` is empty for
single-chunk chapters and describes the part position otherwise.

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
- style profile → `{"style_summary": str, "background": str}` (stored in
  novel_info.json)
- glossary merge → single entry `{"source", "translation", "definition", "category"}`

## Catalogues (`assets/catalogues/` in the skill)

```jsonc
{ "language": "zh", "name": "...",
  "terms": [ { "source": "练气", "variants": ["練氣"], "translation": "Qi Condensation",
               "category": "level", "definition": "First major realm of cultivation." } ] }
```

A catalogue for a new language (ja/ko) is just another JSON file with the
right `language` — `init`/`seed` pick up every catalogue matching
`source_lang`.
