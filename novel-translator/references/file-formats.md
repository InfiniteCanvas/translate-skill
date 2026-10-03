# File Formats — novel-translator

Schemas and contracts for every file in a translation project. The scripts in
`scripts/lib/` are the source of truth for behavior; this file is the source of
truth for *shapes* the user (or the agent) is expected to read, edit, or
hand-fix. Every read of a user-editable file is decoded BOM-tolerant
(`utf-8-sig`), so a UTF-8 BOM left by a hand editor never breaks a read
and never registers as template drift. Closed list of what that covers:
every project JSON file; all chapter reads (including `util replace`'s);
`style.md`; the `styles/` presets (skill and project copies alike);
`review-report.md`; every template read (the pipeline's `_load_template`
project-copy-or-skill-fallback, the profile job's `style_profile.md`,
and both review tiers' templates); and both sides of migrate's template
drift comparison.

## Project layout

```
<project>/
├── config.json          settings: languages, LLM providers, thresholds
├── novel_info.json      book metadata (title, author, source, tags)
├── style.md             active style guide (copied from a preset at init, hand-editable)
├── chapters.json        manifest: every chapter, its order and status (rebuilt by init and sync)
├── glossary.json        translation glossary (seeded + model-grown)
├── tn_history.json      translation-note history (powers the 10-chapter rule)
├── story_state.json     rolling story-so-far recaps (one entry per chapter; injected as translator context)
├── review-report.md     indexed review findings (`review glossary`: machine-applicable vs needs-manual-review sections; `review notes`: advisory note-quality findings; frontmatter counts; regenerated per run by whichever tier ran last)
├── source/              Chapter_NNNN[a].md — untouched source chapters
├── draft/               working area per chapter (see Draft artifacts)
├── translated/          finalized chapters, exactly what the epub is built from
├── notes/               translator's-note sidecars (notes/<stem>.json) + dropped-candidate review artifacts (notes/<stem>.dropped.json)
├── covers/cover.jpg     scraped or generated cover
├── templates/           per-project copies of the prompt templates (editable)
├── styles/              optional per-project style presets (add/override .md files)
├── export/              built epubs
├── .git/                git repository (created by `init`, backfilled by migrate v003) — skill-managed, see Git history
├── .gitignore           skill-managed ignore rules for transient/rebuildable paths (see Git history)
└── logs/                llm-*-<command>-<pid>.jsonl (one LLM trace per project per CLI invocation, newest log_llm_keep_runs kept); epub-build.log (background epub-build output)
```

Chapter file names must match `Chapter_NNNN.md` (1-4 digit zero-padded
number, matching is case-insensitive), optionally with a letter suffix for
extras/bonus chapters:
`Chapter_0042a.md` sorts between `Chapter_0042.md` and `Chapter_0043.md`.
Chapter order = position in the file list sorted numerically by the parsed
`(number, suffix)` — so `Chapter_999` sorts before `Chapter_1000`; that order
is written into each source file's frontmatter and into `chapters.json` as
`order` (0-based), and ALL chapter-distance logic (the translation-note gap)
measures distance in `order` units. The manifest is rebuilt by `init` and
`sync`; `sync` re-scans `source/` for added/removed files and preserves
statuses by file name.

## Git history

Every project directory is a git repository. `init` creates the repository
as part of scaffolding (console: `[git] initialized repository`) and makes
the initial `init: scaffold project` commit; migration `v003` backfills
existing projects the same way. From then on every mutating action commits
(`lib/vcs.commit` is the single gate), so `git log` doubles as a labeled
backup of the project. A repository the skill did not create is treated as
foreign: commit() refuses to touch it — `git add -A` would sweep the user's
own pending changes into a skill-labeled commit (see the console lines
below). The skill recognizes its own repositories by a
`.git/novel-translator-managed` marker written at creation, or — for
repositories created before the marker existed — by a `.gitignore` that
carries ALL FOUR original rules (`draft/`, `logs/`, `export/`, `*.tmp`) and
nothing but rules the skill writes (so an ignore file predating a later rule
such as `covers/` still counts as the skill's own, while one carrying a
single foreign rule does not); anything else is the user's own repository.
`build-epub`, the auto-build, and `glossary search` / `glossary count`
produce nothing — `export/`, `logs/`, and `covers/` are gitignored, and
search/count are read-only.

`.gitignore`, written at repository creation:

```
# Transient pipeline state and rebuildable artifacts (skill-managed).
draft/
logs/
export/
covers/
*.tmp
```

Commit subjects, verbatim — one commit per action, only when something
actually changed:

| Action | Commit subject |
|---|---|
| fresh `init` | `init: scaffold project`, then `init: backfill and seed` |
| `init --force` over an existing repository (history is kept) | `init: reinitialize project` (twice: the scaffold, then the backfill/seed stage) |
| `sync` that changed anything | `sync: rescan source` |
| finished chapter — `translate` and `retry`; skipped chapters commit nothing | `translate: chapter NNNN (translated)` / `translate: chapter NNNN (needs-review)` |
| `mark` | `mark: <file> -> <status>[, ...]` |
| `tn` (when at least one chapter changed, or only `tn_history.json` drifted — kept notes bump `times`/`last_order` even when every sidecar is identical) | `tn: re-check notes` |
| `seed` | `seed: N glossary term(s)` |
| `profile` | `profile: regenerate style profile` |
| `migrate`, per applied step | `migrate: vNNN <DESCRIPTION>` |
| `migrate` maintenance pass whose template refresh changed files | `migrate: refresh templates` |
| `migrate` maintenance pass that (re)created a missing repository (v003's `git init` can have failed once; the templates subject wins when both happened) | `migrate: backfill git repository` |
| `review glossary` (the report is committed even when findings remain) | `review: glossary audit` / `review: glossary audit (N fix(es) applied)` |
| `review notes` (the report is committed even when findings exist — the tier is advisory-only, findings never fail the run) | `review: notes audit` |
| `util replace` | `util replace: '<src>' -> '<dst>'` |
| `glossary replace` | `glossary replace: '<src>' -> '<dst>'` |
| `glossary set` / `glossary merge` / `glossary retire` | `glossary set: <term>` / `glossary merge: '<removed>' into '<kept>'` / `glossary retire: <term>` |

`review fix` makes NO commit of its own: each spawned glossary subcommand
commits its own action.

Console lines — `[git]` is part of the stable marker vocabulary:

- `[git] initialized repository` — the repository was created (`init` /
  migrate v003).
- `[git] committed <short-sha> <subject>` — printed only when the commit
  actually happened. A clean tree, a disabled `git_commits`, a non-repo
  directory, or a missing git binary is a SILENT no-op: a versioning
  problem never breaks a translation run, mirroring epubcheck's
  missing-docker tolerance. A foreign repository is also a no-op — but
  not a silent one (next bullet).
- `[warn] git: skipping commits - <dir> looks like a foreign repository
  (no skill marker, and its .gitignore is not the skill's alone); add the
  skill's .gitignore rules to let the skill manage it, or set git_commits:
  false` — the project dir is already a repository the skill does not
  recognize (no `.git/novel-translator-managed` marker, and a `.gitignore`
  that is not the skill's rules alone), i.e. the user's own; commit() refuses
  so their pending changes are never swept into a skill-labeled commit.
  Printed at most once per directory per process.
- `[warn] git not found; project history disabled` — init / migrate on a
  machine without git.
- `[warn] git init failed: <reason>` / `[warn] git add failed: <reason>` /
  `[warn] git commit failed: <reason>` — best-effort, at most one line.
- `[warn] git failed: <reason>` — any unexpected failure (binary vanishing
  between lookup and spawn, dead drive, half-deleted `.git`); `commit()` and
  `ensure_repo()` never raise, so this is at most one line per call. Every
  git subprocess is killed after 300s; a timeout surfaces here as
  `git command timed out after 300s: git ...`.

Git config is set local to the repository, never the global config, at
repository creation: `user.name=novel-translator` /
`user.email=novel-translator@localhost` when no identity is configured, and
`core.autocrlf=false` when unset (project files are LF).

Off switch: `git_commits` (default true) in config.json — set it false to
keep a project un-versioned. Commits (and their `[git] committed` lines)
become silent no-ops; nothing else changes and no error is raised.

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
count. This is the anti-hallucination backbone of the whole pipeline. A
body with no content lines (empty file, or nothing but blank lines) never
enters the pipeline: `[Chapter_NNNN] [warn] <file>: source chapter has no
content - marked needs-review` — the chapter is marked `needs-review`, no
LLM call.

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
    "recap":       { "...same shape, temperature 0.2" },   // rolling story-so-far recap generation (one cheap call per chapter; point it at a cheap model)
    "profile":     { "...same shape, temperature 0.3" }   // style-profile generation (--style auto / `profile` only)
  },
  "seed_min_count": 3,           // catalogue term must appear >= N times in source/ to seed
  "min_term_occurrences": 3,     // minimum novel-wide occurrences for GLOSSARY_EXPAND to add a brand-new term (0 disables the gate); also the default threshold for `glossary count`
  "min_term_coverage": 0.25,     // ADVISORY usage floor: below ceil(coverage*src) warns; 0 renderings with src>=2 is a drift signal handed to the FAITH reviewer
  "fuzzy_max_distance": 2,       // Levenshtein tolerance for single-word targets of >= 5 letters; multi-word phrases match case-insensitively with hyphen/space equivalence plus an optional inflection on the final word
  "glossary_auto_cleanup": true, // balance drift signals: retire mundane terms via a cleanup judgment; kept signals go to the FAITH reviewer; false = skip the judgment
  "git_commits": true,           // commit every mutating action to the project's git repository (created by `init`, backfilled by migrate v003; lib/vcs.commit is the single gate); false = keep the project un-versioned
  "tn_gap_chapters": 10,         // re-annotate a term only after > N chapters of distance
  "tn_keep_low_confidence": false, // keep threshold:"low" notes instead of dropping them (default drops)
  "auto_build_epub": true,      // rebuild the epub in the background after every translated chapter (serialized; final build at batch end); false = manual `build-epub` only
  "max_attempts": 3,             // translation attempts before needs-review
  "contextual_glossary_cap": 200, // safety valve only — every glossary term present in the chapter goes in
  "max_new_terms_per_chapter": 15,
  "max_notes_per_chapter": 10,
  "translate_max_output_tokens": 8192, // per-call OUTPUT cap (card recommends 4k-8k) + packing budget for splitting: parts close past floor(0.8*this)-256 of per-line estimated cost; input context is never limited
  "style_sample_chapters": 4,    // chapters sampled (at random) for style-profile generation (--style auto only)
  "style_sample_chars": 12000,   // rough source-character budget for the sample (--style auto only)
  "log_llm": true,               // full request/response LLM trace; false disables the LLM trace lines only
  "log_llm_keep_runs": 5,        // one llm-*.jsonl per project per CLI invocation; older logs pruned to the newest N (by mtime)
  "review_batch_size": 40,       // entries per `review glossary` / `review notes` model review call; `--batch-size` overrides per run
  "review_report_path": "review-report.md", // advisory review report filename, relative to the project dir (written by `review glossary` / `review notes`, read back by `review fix`)
  "version": 7                   // project version (see Migrations) — written by `init` (fresh projects are born current) and `migrate` (stamped after each successfully applied step) ONLY, never merged from DEFAULTS — the raw on-disk value is the source of truth; a config.json without the key is version 0
}
```

- `base_url` includes `/v1` (OpenAI-compatible). `model: null` means "ask
  `/models` and use the first model" — resolved at runtime (cached per
  base_url + resolved auth identity, so two jobs sharing a URL with
  different keys keep distinct resolutions), works with any sglang/vLLM
  server.
- Each job can point at a **different provider**: keep `translator` on the
  translation model, later point `reviewer` at a stronger model. Any job block
  you omit inherits the `translator` block.
- **Sampling knobs**: `temperature` and `top_p` (plus optional `top_k`,
  `repetition_penalty`) are per-provider and passed through to the server.
  Only `translator` carries `top_p` by default (1.0, per the model card);
  the other jobs send no `top_p` (the server default applies). Translator
  defaults follow the Hy-MT2 model card (0.7 / 1.0) — tune to taste
  per project; translation quality at different temperatures is subjective.
- Language codes (`zh`, `en`, ...) are mapped to full names ("Chinese",
  "English") automatically at prompt-build time, per the model card's
  guidance. Templates always see full names.
- No API keys are stored; if the endpoint needs one, add `"api_key": "..."`
  (sent as `Authorization: Bearer`).
- Numeric config values (the top-level thresholds and sampling knobs listed
  above) are validated: a non-numeric value or `null` fails with
  `[FAIL] config key '<key>' must be a number (got <value!r>)` and exit 2
  (`<value!r>` is the Python repr of the value that was read). Provider
  sampling knobs (`temperature`, `top_p`, `max_tokens`) are read per request
  and are not validated the same way.
- A provider `max_tokens` below `translate_max_output_tokens` draws a
  once-per-run warning — `[warn] config: providers.translator.max_tokens
  (N) is below translate_max_output_tokens (M) - retries cannot raise the
  output cap` — the truncation-retry escalation caps at the provider's
  `max_tokens`, so retries cannot raise the output cap past it.

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
  "cover_placeholder": false,                 // optional; true when covers/cover.jpg is the generated placeholder rather than a scraped image
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
empty. `cover_placeholder` is true when the local cover image is a
generated placeholder rather than a scraped one — delete
`covers/cover.jpg` to force a re-scrape. Recording that flag is
best-effort: a failure to write it warns once
(`[warn] cover: recording placeholder state failed: <reason>`) and never
fails the build.

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
`mark --status` accepts exactly `translated`, `pending`, or `needs-review`;
`in-progress` is a real status but only the pipeline writes it — `mark`
refuses to. Status updates after ASSEMBLE are best-effort: the write
retries through Windows file-lock contention with exponential backoff
(~3.1s in total) and on exhaustion prints
`[warn] manifest update failed for <file>: <reason> - chapter file is
written; status stays in-progress`, leaving the status `in-progress`.

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
      "category": "level",               // place|person|org|skill|technique|level|state|item|honorific|unit|other — `unit` is guide-only: injected into the translation prompt as a rendering guide, ignored by the balance checker (see below)
      "origin": "seeded",                // seeded (catalogue) | model (proposed during translation)
      "first_seen_chapter": 12           // order index where a model-proposed term first appeared
    }
  ],
  "retired": ["灵气"]                    // optional; sources removed from the glossary (balance auto-cleanup, glossary merge/retire) — the entry's CANONICAL source, plus any variant spelling the user supplied; seed and glossary expansion skip them, delete a source here to allow re-adding
}
```

- The balance check counts `source`+`variants` occurrences in the source text
  and `translation`+`alt_translations` occurrences in the translated text
  (stemmed, case-insensitive word/phrase matching with Levenshtein tolerance 2
  for words ≥ 5 letters; exact substring match for CJK targets), cross-entry
  longest-first — a term nested inside a longer glossary compound (仙界
  inside 修仙界) is credited to the longer term only. Entries whose
  `category` is `unit` are skipped entirely — they are injected into the
  translation prompt as rendering guides (contextual glossary), but their
  short polysemous source strings (里 in 这里/里面, 寸 in idioms) make
  counting pure noise, and emitting no signals also puts them beyond
  auto-cleanup retirement. Assigning `unit` to an entry with a non-empty
  translation warns (`[warn] glossary: '<source>' has a translation but
  category 'unit' (guide-only: balance checks skip it)`) — from model
  proposals and `glossary set` alike. An entry with no `translation` yet
  (a minimal
  hand-added stub) is skipped too — without a canonical rendering there is
  nothing to count. The check is FULLY
  ADVISORY: no condition fails a chapter. Falling below the usage floor
  `ceil(min_term_coverage × src)` (default 25%) is a console warning —
  capped at the first 5 per chapter, the remainder summarized as
  `[warn] ... and N more (see logs)` — and exceeding
  `src + max(2, src)` is logged only (over-count stays trace-only). Drift signals — the canonical
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
  `skipped: [{source, field, reason}]`. A `review notes` run logs one
  `notes_review` event — fields `chapters` (contributing chapter file
  names), `units`, `batches`,
  `batch_errors`, `skipped` (file names), `findings` (each `{chapter, term,
  note, line?, idx, kind, severity, reason, suggestion, origin}` — `origin`
  is `deterministic` for the no-model misanchored tier, `model` otherwise;
  `line` is the anchor-resolved translated-line index, absent on
  unresolvable notes).
- Hand-editing entries between runs is safe and encouraged — the file is read
  fresh before every chapter. Hand-added entries need at least `source` and
  `translation`. A corrupt glossary.json fails whatever command reads it
  cleanly (one `[FAIL]` line, exit 2 — never a traceback); `status` degrades
  instead to a `glossary: [warn] unreadable (...)` row in place of the term
  count. `glossary replace` (which rewrites the old rendering in
  translated chapters before saving the new `translation`) prunes that
  rendering from the entry's `alt_translations` by default — a stale alt
  would keep the balance check counting the old rendering as valid,
  masking drift; `--keep-alt` leaves alt_translations untouched.

## review-report.md

Regenerated by every `review glossary` run at `<project>/review-report.md`
(filename: config `review_report_path`; overwritten — a clean run writes
a report that says so; console: `[glossary] report: <path>`). A run's
console sequence: the header `[glossary] review: N entries (B model
batch(es) of up to S)` prints before the model calls, `[glossary]
reviewing batch i/n` before each batch, `[glossary] warn batch i/n
review failed - <error>` after a failed batch, and — after the
per-finding lines — the post-run summary `[glossary] review: N entries,
W warn / I info findings`, a different line from the header that counts
OUTSTANDING findings (fixes `--fix` applied this run drop out). A `review
notes` run writes the SAME file with a different format (see
Notes-review reports below) — the report is per-run and per-tier: whichever
review ran last owns the file, so a notes run replaces a previous glossary
report and vice versa. The glossary form is the indexed, agent-actionable view of
the findings the `glossary_review` trace event records: run the review,
then either tell an agent "fix items 1,4,5 in review-report.md doing what
was suggested" or run `review fix --glossary review-report.md` to apply
every finding in the report's `Machine-applicable` section offline in one
command. **Delete any `- Command:` bullet to veto that finding** —
`review fix` only runs what the report lists.

````markdown
---
report_type: glossary-review
generated: 2026-09-08T12:34:56+00:00
generated_by: review glossary
source_lang: zh
target_lang: en
entries_reviewed: 120
batch_errors: 0
glossary_digest: 3f9a2c1e77b4
outcome:
  warn: 2
  info: 1
machine_applicable: 2
manual_review: 1
manual_review_indices: [3]
---

# Glossary Review Report

- Generated: 2026-09-08T12:34:56+00:00
- Generated by: `review glossary`
- Languages: Chinese -> English
- Entries reviewed: 120 (3 model batch(es))
- Outcome: 2 warn / 1 info outstanding

## Machine-applicable (apply with `review fix`)

### [1] warn / mistranslation / 天雷宗

- Reason: ...
- Suggestion: ...
- Tier: heuristic
- Entry:
  ```json
  { ...full glossary entry... }
  ```
- Action: ...
- Command: glossary replace --source '天雷宗' --translation 'Heavenly Thunder Sect'

### [2] info / category / 裴家村
...

## Needs manual review (decide yourself or hand to an agent)

### [3] warn / duplicate / 青云剑

- Reason: ...
- Tier: model
- Entry:
  ```json
  { ... }
  ```
- Action: ...

## Fixed automatically (--fix)
## Fixes skipped (need a decision)
## Next steps
````

The body bullets take conditional suffixes: `- Entries reviewed:` grows
`, N batch error(s) -- findings from failed batches are missing` when
model batches failed, and `- Outcome:` grows `, N fixed automatically`
when `--fix` applied fixes that run — both omitted when zero.

Frontmatter fields (all always present; `0` / `[]` when empty):

| Field | Meaning |
|---|---|
| `report_type` | always `glossary-review` |
| `generated` | UTC ISO-8601 timestamp (same value as the `- Generated:` body bullet) |
| `generated_by` | `review glossary` or `review glossary --fix` — plain text, no backticks (the body `- Generated by:` bullet keeps the backticked form and the provenance role) |
| `source_lang` / `target_lang` | raw language codes from the project's config.json (the body `- Languages:` bullet keeps human-readable names) |
| `entries_reviewed` | number of glossary entries reviewed |
| `batch_errors` | number of model batches that failed (findings from failed batches are missing) |
| `outcome.warn` / `outcome.info` | outstanding warn/info counts (same numbers as the `- Outcome:` body bullet) |
| `machine_applicable` | count of machine-applicable findings |
| `manual_review` | count of manual-review findings |
| `manual_review_indices` | the `[N]` indices of the manual-review findings, e.g. `[3]` — `[]` when there are none; an agent can read just the frontmatter to know whether/which manual items exist |
| `glossary_digest` | first 12 hex chars of the SHA-256 of glossary.json's bytes at generation time; `null` when the project has no glossary.json — `review fix` compares it against the live glossary and refuses a stale report (below) |

`## Next steps` footer bullets (verbatim):

- Run `review fix --glossary review-report.md` to apply every finding in `Machine-applicable` (one offline command per `- Command:` bullet).
- For findings under `Needs manual review`, decide yourself or hand that section to an agent (e.g. "work through the Needs manual review section in review-report.md").
- Fix remaining findings by editing glossary.json (hand edits are safe -- it is re-read before every chapter).
- Delete junk entries outright and add their source to the top-level "retired" list so seed/GLOSSARY_EXPAND will not re-add them.
- Re-run `review glossary` to confirm the report comes back clean (exit 0).
- If chapters were already translated with a wrong rendering, re-run `retry --chapters N` after fixing.

Partition rule: a finding lands in `Machine-applicable` iff its fix is
fully determined by its structured fields — exactly the mapping that emits
its `- Command:` bullet (`command_for_finding()` in `lib/review.py`), so
every machine-applicable finding carries a `- Command:` line and no
manual-review finding has one; model-tier duplicate/variant findings,
suggestion-less field kinds, and `other` are manual-review items. Each
finding is a `### [N] severity / kind / source` heading with `- Reason:`,
`- Suggestion:` (only when one exists), `- Tier:` (`heuristic` | `model`),
`- Entry:` (the FULL glossary entry as pretty JSON, so no glossary.json
lookup is needed), `- Action:`, and — in the `Machine-applicable` section
only — `- Command:`. Section order: `Machine-applicable` first, then
`Needs manual review`; within each, warn before info, source-ascending
within a severity; a section with no findings is omitted entirely. The
`--fix` sections keep their shapes: `## Fixed automatically (--fix)` lists
"`source`: field 'old' -> 'new'" lines, `## Fixes skipped (need a
decision)` lists "`source` (field): reason" lines. A clean run writes the
frontmatter with all-zero counts plus the existing `No outstanding
findings -- the glossary is clean.` line and no finding sections. Indices
`[1]`, `[2]`, ... number OUTSTANDING findings only, continuously in
reading order — the `Machine-applicable` section first, then `Needs
manual review`; findings resolved by `--fix` this run are excluded from
the numbering and listed under "Fixed automatically" instead. Each
**Action** line is the model-written
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

The `- Command:` bullet — one on every `Machine-applicable` finding, none
in `Needs manual review` — is the contract `review fix` honors. Every
value is `shlex.quote()`-escaped, so CJK source terms and spaces survive
copy-pasting. The bullet is emitted only when the fix is **fully
determined by the finding's structured fields** (see
`command_for_finding()` in `lib/review.py`); the closed mapping is:

| Finding kind + data                                  | Emitted command |
|---|---|
| `mistranslation` / `wrong_language` with suggestion  | `glossary replace --source S --translation T` |
| `collision` with suggestion                          | `glossary replace --source S --translation T` |
| `definition` with suggestion                         | `glossary set --source S --definition D` |
| `category` with a suggestion that is a glossary category | `glossary set --source S --category C` |
| heuristic `variant` (has `variant_to_remove`)        | `glossary set --source S --remove-variant V` |
| heuristic `duplicate` (has `merge_with`)             | `glossary merge --keep M --remove S` |
| `mundane`                                            | `glossary retire --source S` |
| model-tier `duplicate` / `variant`, suggestion-less field kinds, `other`, a `category` suggestion outside the glossary categories | _(no Command bullet — decision item)_ |

`review fix` pre-validates each command before running it: a `glossary
replace` / `set --translation` whose suggested value still contains
source-script characters for a CJK-source entry is skipped in-process
(console: `[review fix] skipped [N]: suggestion not in target language`)
and counted as needing a decision — the same guard `review glossary
--fix` applies to its own fixes. A `- Command:` bullet carrying
`--project` (as `--project X` or `--project=X`) is likewise never run:
the executor prepends its own `--project`, and a hand-added one would
silently retarget the command (console: `[review fix] skipped [N]:
command overrides --project`), counted the same way. Two commands
targeting the same glossary entry with the same verb+target conflict
(where a `glossary set` verb is its edited field, so two `set` commands
editing different fields of one entry both run) —
the first one queued wins, later ones are skipped (console:
`[review fix] skipped [N]: conflicting command for '<source>' (already
queued)`). Before running anything, `review fix` compares the report's
`glossary_digest` frontmatter against the live glossary.json; a mismatch
(the glossary changed since generation) is refused with exit 1 —
`[review fix] report is stale (glossary changed since generation) -
regenerate with review glossary` — unless `--stale-ok` is passed. A
report without the field (or with `null`) is never treated as stale, so
old or hand-written reports stay runnable. Each
spawned command runs as a subprocess killed at a 1800s timeout
(`[review fix] command timed out after 1800s: <command>`); a command
that changes nothing prints `[glossary] noop: <detail>` and counts as a
no-op.

Legacy reports (the pre-split format: no YAML frontmatter, findings
grouped by severity under `## Warnings (fix before translating further)` /
`## Info (optional improvements)`, possibly no `- Command:` bullets, and
the older `- Command: review glossary` header that records the generating
command) remain fully supported: `review fix` synthesises the same
commands from the heading + `- Suggestion:` bullet + the two exact
heuristic reason templates, so an old report needs no review re-run.
Header bullet history: the `- Generated by:` form above applies to
reports written by current and future versions; `review fix` ignores the
legacy `- Command: review glossary` header because its argv
(`review glossary`) does not start with a supported glossary verb.

### Notes-review reports (`review notes`)

`uv run scripts/translate.py review notes --project . [--chapters SPEC]
[--batch-size N]` audits EXISTING translator's notes — the goal of the
whole notes system is notes that add context or explain context lost in
translation, so the audit flags notes that fail to earn their place.
Selection: every chapter with a `notes/<stem>.json` sidecar in manifest
order (chapters without one are skipped silently), or the `--chapters`
spec (same SPEC semantics as `translate --chapters`) when given; a chapter
whose translated or source file is missing or unreadable warns and skips
(`[warn] <file>: translated chapter missing - skipped` and kin). Per note, the
stored line index is re-resolved with the epub builder's anchor rules (the
stored `line` wins while its translated line still starts with the
stored `anchor`; else the first line starting with the anchor; else the
note is unresolvable). Two tiers merge into one findings list:

- **Deterministic (no model)**: an unresolvable note becomes a
  `misanchored` warn on the spot — reason `anchor no longer matches any
  translated line`, suggestion `re-attach or delete the note`.
- **Model tier**: resolvable notes become review units — `{idx, chapter,
  line, term, note, category, translated_line, source_line,
  context_before, context_after}` (source line at the resolved index,
  empty string past a shorter source body; context = the ±2 translated
  lines) — flattened across chapters in manifest order and judged in
  `review_batch_size` batches (CLI `--batch-size` overrides) by the
  `reviewer` provider (temperature 0.0) through `templates/notes_review.md`.
  Judgment kinds, a closed vocabulary: `restates` (the note adds nothing
  beyond what the translation already says), `overexplains` (common
  knowledge or inferable from context — fails the comprehension
  threshold), `wrong` (the note misexplains the source term), and
  `misanchored` (attached to the wrong line / the term does not appear
  there). Rows outside the closed vocabulary (unknown idx / kind /
  severity, empty reason) are dropped with a
  `[notes] warn dropped finding: ...` line; one failed batch is
  reported and skipped, never fatal.

The run is **advisory-only**: exit 0 on a completed run regardless of
finding count (no `--fix`, no exit-1-on-warns — that is the glossary
tier's contract; usage errors still exit 2: `--fix`, `--batch-size` < 1,
a `--chapters` spec matching no chapter, or any flag that does not
apply to the subject —
`[FAIL] --<flag> does not apply to 'review <subject>'`), and the report carries
**no `- Command:` bullets anywhere** — the closed command vocabulary stays glossary-only. Fixes are
hand edits to `notes/<stem>.json` (delete, reword, or re-attach the
note); `review fix` pointed at a notes report fails cleanly with exit 2
(`report has no machine-applicable commands`) — no notes kind maps to a
glossary command, by design. Console: `[notes] reviewing batch i/n` per batch, then
`[ok] review notes: N findings (restates X, overexplains Y, wrong Z, misanchored W) -> <report path>`
and, when N > 0,
`[warn] review notes: fixes are hand edits to notes/<stem>.json (delete, reword, or re-attach the flagged note)`.
A run with no sidecars anywhere prints
`[ok] no chapter notes found - nothing to review`, writes no report, and
exits 0 (mirroring `review glossary` on an empty glossary) — that `[ok]`
line means genuinely no sidecars: when chapters were selected but every
one was skipped, `[warn] no chapter notes reviewed: N chapter(s) skipped
(see failures above)` prints instead. The run
commits `review: notes audit` and logs a `notes_review` trace event.

Report shape (same file the glossary tier writes, same per-run overwrite
semantics — a notes run replaces a previous glossary report):

````markdown
---
report_type: notes-review
tier: notes
generated: 2026-09-30T12:34:56+00:00
generated_by: review notes
source_lang: zh
target_lang: en
chapters_reviewed: 12
notes_reviewed: 40
batch_errors: 0
outcome:
  warn: 2
  info: 1
kinds:
  restates: 1
  overexplains: 1
  wrong: 0
  misanchored: 1
---

# Notes Review Report

- Generated: 2026-09-30T12:34:56+00:00
- Generated by: `review notes`
- Languages: Chinese -> English
- Chapters reviewed: 12 (40 note(s), 1 model batch(es))
- Outcome: 2 warn / 1 info findings (restates 1, overexplains 1, wrong 0, misanchored 1)

## Notes findings

### [1] warn / restates / Chapter_0042.md / 灵根

- Note: Spirit root: innate cultivation aptitude.
- Line: 17
- Reason: the note paraphrases the translated line
- Suggestion: ...
- Tier: model

## Next steps
````

Frontmatter fields (all always present; `0` when empty): `report_type`
(always `notes-review`), `tier` (always `notes`), `generated`,
`generated_by` (`review notes`), `source_lang` / `target_lang` (raw
codes), `chapters_reviewed` (chapters that contributed notes),
`notes_reviewed` (units sent through the model tier), `batch_errors`,
`outcome.warn` / `outcome.info`, and `kinds.<kind>` per-kind counts.
The `- Chapters reviewed:` bullet takes the same conditional batch-error
suffix as the glossary report — `, N batch error(s) -- findings from
failed batches are missing` — omitted when zero.
Findings are `### [N] severity / kind / chapter / term` headings, warns
before infos then reading order, with `- Note:`, `- Line:` (the resolved
index; absent on unresolvable notes), `- Reason:`, `- Suggestion:` (only
when one exists), and `- Tier:` (`deterministic` | `model`) bullets. The
`## Next steps` footer states the hand-edit workflow and the overwrite
caveat: hand-edited sidecars are overwritten if the `tn` re-check command
later regenerates that chapter's notes. A clean run writes the all-zero
frontmatter plus `No findings -- every reviewed note earns its place.` and
no findings section.

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
step. `v001` materializes merged config
defaults onto disk (keys introduced after the project's init, e.g.
`glossary_auto_cleanup` and `min_term_coverage`, plus provider-job
normalization; user-set values always preserved) and copies shipped
templates missing from the project's `templates/` dir without prompting
(non-destructive; the runtime fallback would cover them anyway). `v002`
materializes the review command defaults (`review_batch_size`,
`review_report_path`) with identical behavior — the same add-only
deep-merge (only keys missing from the project's config.json are added;
user-set values always preserved) plus the same template sync. `v003`
(DESCRIPTION: `git history: materialize git_commits default, init the
project repo`) folds the `git_commits` default into the project's
config.json the same add-only way and runs the same template sync, then
`git init`s the project directory (writing `.gitignore` and the
local-only git config — see Git history). The step itself commits
nothing: `migrate` commits once per applied step AFTER stamping the
config version, so the repo's first commit captures the fully migrated
state. `v004` (DESCRIPTION: `add min_term_occurrences (novel-wide
significance gate for glossary expansion)`) materializes the
`min_term_occurrences` default the same add-only way and runs the same
template sync. `v005` (DESCRIPTION: `restrict glossary terms to named
entities, named actions, and name-bound titles`) runs the template sync
only — it adds no config key and never touches config.json, refreshing
the shipped glossary templates so glossary terms are restricted to named
entities, named actions, and titles bound to a name (idempotent). `v006`
(DESCRIPTION: `transliterated measurement units: conversion-note guidance
and the guide-only unit category`) is also templates-only — refreshing
`tn_generate.md` (a transliterated measurement unit always warrants a
conversion note at its first chapter occurrence) and `glossary_review.md`
(`category: "unit"` entries are exempt from the mundane judgment), backing
the catalogue-shipped unit terms that the balance checker ignores
(idempotent). `v007` (DESCRIPTION: `materialize the recap provider job;
ship recap.md and notes_review.md; refresh tn_generate.md`) materializes
the new `recap` provider block into the project's config.json (no
top-level key is added, so it reports the same provider-blocks-normalized
line as v001's no-new-keys case; user-set values always preserved, and
the omitted `recap` job inherits the project's `translator` block) and
runs the same template sync — shipping the two new templates (`recap.md`
for the rolling story recap, `notes_review.md` for the `review notes`
tier) and refreshing the rewritten `tn_generate.md` (glossary-aware
categorized annotation with the code-enforced cap; every pre-v007
project's copy reads as drifted) (idempotent).
`--dry-run` writes nothing and reports
`[git] would initialize the repository (a real run commits after each
migrate step)` (cmd_migrate prefixes step lines with `[dry-run] `). A
template that exists but differs from the shipped one is asked about
interactively, one prompt per template — `templates ~ <name>.md
differs from the shipped copy - overwrite it? [y/N]`: Enter/n keeps
the project's version (the default; it may be user-customized), y
overwrites it with the shipped copy. `--force` answers yes to all
prompts (no prompting), and
non-interactive runs (stdin not a TTY: piped, scripted, CI) never
prompt and never block — differing templates are kept with a `[warn]`
line (`--dry-run` reports differing templates without prompting or
writing). A project already current is not a bare no-op: `migrate` runs
a read-only-when-clean template maintenance pass (same prompt rules)
that leaves the version stamp untouched, and best-effort backfills a
missing repository (the only retry when v003's `git init` failed once
yet version 3 was still stamped) — the chain gates structural
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
`tn_keep_low_confidence` is true (the discards are recorded in the
chapter's `notes/<stem>.dropped.json`, next section). A missing file is
simply an empty history;
a malformed one is discarded — note-gap tracking restarts empty, never a
crash — with one `[warn] tn_history.json unreadable (<reason>) - resetting
note-gap tracking` line so the reset is never silent.

## story_state.json

```jsonc
{
  "chapters": {
    "Chapter_0042": {
      "recap": "Lin Feng reached Foundation Establishment under Elder Wu's tutelage...",  // running recap, <= 120 words, target language, third person
      "updated_at": "2026-09-30T12:00:00+00:00"   // UTC ISO-8601, seconds
    }
  }
}
```

The rolling "story so far" recap: one entry per translated chapter, keyed
by the file stem. After a chapter's ASSEMBLE succeeds, ONE `recap`-provider
call (`templates/recap.md`) condenses the previous recap plus the
just-translated chapter into a fresh ≤ 120-word recap stored as the
chapter's own entry (console: `[Chapter_NNNN] [init] recap`); the next
chapter's TRANSLATE / FAITH / TN_GENERATE prompts receive it inside their
`[Background Information]` frame, as `Story so far (auto-generated recap
of the preceding chapters):` followed by the text. When the predecessor's
entry is missing — chapters translated before the feature existed, or a
crash that landed between ASSEMBLE and the record — it is backfilled with
exactly ONE extra recap call before translation starts (console:
`[Chapter_NNNN] [init] recap (backfill Chapter_PPPP.md)`), anchored on the
nearest EARLIER existing entry, never a recursive chain. Failures are
advisory and never fail or gate a chapter —
`[Chapter_NNNN] [warn] recap backfill failed for <prev>: <reason>` /
`[Chapter_NNNN] [warn] recap generation failed for <file>: <reason>` — the
chapter translates without a recap and the next run retries the backfill.
`updated_at` is managed by the tool. Reads are BOM-tolerant; a malformed
file is discarded — recaps start fresh, never a crash — with one
`[warn] story_state.json unreadable (<reason>) - recaps start fresh` line.
Entries whose chapter stem no longer exists in `chapters.json` are pruned
at load time, so a dropped or renumbered chapter stops feeding its recap into
later chapters; a missing, empty, or unreadable manifest disables pruning
(recaps are never destroyed on uncertain grounds). A stem still present in
the manifest keeps its recap even when the file behind it was replaced —
nothing recorded distinguishes a reused stem, and the recap is advisory
context that the chapter's own re-translation refreshes. Staleness semantics:
retranslating chapter N refreshes only N's own entry;
entries after N stay as built until those chapters are themselves
retranslated — the recap is advisory context, never a gate. Everything the
pipeline writes here lands inside the chapter's own
`translate: chapter NNNN (translated)` commit (run_range commits after
run_chapter returns); there is no separate commit.

## notes/<stem>.json (translator's-note sidecar)

```jsonc
{
  "chapter": "Chapter_0042.md",
  "updated_at": "2026-09-06T12:00:00+00:00",
  "notes": [
    { "line": 17, "term": "筑基", "category": "cultural",
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
existed), and audited (read-only) by `review notes`. The `tn` re-check
treats an annotator response of zero notes for a chapter whose sidecar
holds notes as a failed evaluation, not an empty result: the sidecar and
the translated markdown are left untouched (`[tn] <file>: annotator
returned 0 notes for a chapter with N note(s) - keeping existing
sidecar`). Each note is exactly `{line, term, note, category, anchor}`:
`line` is a
0-based index into the translated body lines as normalized by
`read_chapter` (leading/trailing blank lines stripped), and `anchor`
snapshots the first 80 characters of that line so the epub build can
re-resolve the note onto the right paragraph after hand-edits shift line
numbers — the stored index wins when its line still starts with the
anchor, else the first line starting with the anchor wins, else the note
is dropped with a warning. `category` says what kind of context the note
carries — `cultural` | `idiom` | `wordplay` | `honorific` | `unit` |
`other` — normalized on save: a missing, unknown, or non-string value
(hand-edited or legacy entries included) silently becomes `other`. The
epub builder does not read it (yet); the TN_DEDUP summary line counts by
it. Entries are validated on save (`line` in range,
non-empty string `term`/`note`); a save that keeps zero notes DELETES the
sidecar (absent = no notes). `updated_at` is managed by the tool. A
malformed sidecar is treated as "no notes" — the epub build never crashes on
one — announced by `[warn] <stem>.json unreadable (<reason>) - treating as
no notes`.

## notes/<stem>.dropped.json (dropped-candidates review artifact)

```jsonc
{
  "chapter": "Chapter_0042.md",
  "updated_at": "2026-09-06T12:00:00+00:00",
  "dropped": [
    { "line": 3, "term": "清明", "note": "Tomb-sweeping festival.",
      "category": "cultural", "threshold": "low", "reason": "low_threshold" },
    { "line": 12, "term": "江湖", "note": "The martial world.",
      "category": "cultural", "threshold": "high", "reason": "overflow" }
  ]
}
```

The record of candidate notes the TN filters discarded, so a human can
second-guess the gates without re-running the annotator. Written by the
pipeline's TN_DEDUP stage and by the `tn` command alongside the notes
sidecar, with the same lifecycle: a run that drops nothing DELETES the
file (absent = nothing dropped). `reason` is one of:

| Reason | Meaning |
|---|---|
| `low_threshold` | the model self-assessed the note `threshold: "low"` and `tn_keep_low_confidence` is false (labeled `low-confidence` in the console line) |
| `overflow` | the note was valid but fell past the `max_notes_per_chapter` cap — enforced in code AFTER the gap rule, so a gap-suppressed note never wastes a slot; the prompt asks for severity-ordered entries, so the cut tail is the least severe context loss |
| `invalid` | malformed entry (bad line index, empty/non-string term or note); the same cases the `[warn] dropped ...` lines report |

Fields other than `reason` are best-effort snapshots of the candidate
(invalid entries may carry `null`s). Within-chapter duplicates and
gap-rule suppressions are deliberately NOT recorded (the term is still
noted elsewhere / suppressed on purpose). An `overflow`-dropped term
never consumes the gap window: its `tn_history.json` entry is rolled
back (restored to its pre-call value, or removed when this chapter
introduced it), so a later chapter inside the window can still annotate
it. **Review artifact only — the
epub builder does not read it.** The matching TN_DEDUP console line,
verbatim:

```
[Chapter_0042] [ok] notes: 7 kept (cultural 3, idiom 2, wordplay 1, other 1); 4 dropped (3 low-confidence, 1 overflow) -> notes/Chapter_0042.dropped.json
```

Categories count non-zero entries in the fixed order
`cultural, idiom, wordplay, honorific, unit, other` (all-"other" keeps
show `other K`); a zero-keep chapter prints `[Chapter_0042] [ok] notes: 0
kept` with no parenthetical, and a zero-drop chapter ends the line at the
kept clause (no dropped segment, no file pointer).

## Draft artifacts (`draft/`)

For `Chapter_0001.md` the pipeline creates:

| File | Contents |
|---|---|
| `Chapter_0001.md` | human-readable current translation draft (frontmatter + lines) |
| `Chapter_0001.lines.json` | `{"title": "...", "lines": [...]}` — written per attempt as a debug artifact; nothing reads it back |
| `Chapter_0001.state.json` | pipeline state: `{"stage", "attempt", "title", "lines", "chunks": [[...], ...] or null, "feedback": [...], "notes": [...], "rejected": [...], "updated_at", "pipeline"}` — `pipeline` is the state-schema version; `title`/`lines` hold the draft translation (crash-resume past TRANSLATE); `chunks` holds per-part TRANSLATE progress (below) |

`stage` is one of `TRANSLATE, VALIDATE, BALANCE, FAITH, GLOSSARY_EXPAND,
TN_GENERATE, TN_DEDUP, ASSEMBLE`. `feedback` accumulates everything the gates
rejected (faithfulness reasons, including genuine term drift flagged by
balance signals) and is re-injected into every retry prompt. `rejected` holds
the translated lines of the most recent gate-rejected attempt (null until a
gate first fails) and is re-injected into every retry prompt alongside the
feedback, numbered with the source's 1-based line numbers (line positions can
diverge when the rejection itself was a line-count mismatch).

`chunks` is the per-part TRANSLATE crash-resume state: a list of completed
part translations (each a list of translated line strings, in part order),
appended and saved to the state file immediately after each part validates
(`null` until TRANSLATE starts, `[]` after a gate rejection, absent once
the full `lines` list lands). A crash or Ctrl-C mid-TRANSLATE loses at most
the
in-flight part — the rerun recomputes the (deterministic) packing, prints
`[Chapter_NNNN] [init] resuming translation at part k/n (m lines already
done)`, and calls only the remaining parts (the stashed `title` rides along,
so it is not lost either; when all n parts are already done the loop is
skipped entirely). Cleared to `[]` whenever a gate rejects an attempt (the
rejected snapshot covers all lines, so the retry retranslates the whole
chapter; a TRANSLATE-stage failure keeps its parts), and popped when the full
`lines` list lands (the state file never holds both). If the recomputed
packing no longer matches the saved parts (source or config changed between
runs), the parts are discarded with one `[warn] saved chunks do not match the
current packing (source or config changed?) - retranslating from scratch`
line and the chapter translates from scratch. The same state file also
carries TRANSLATE's oversized-line fail-fast feedback
(`TRANSLATE failed: ValueError: source line N alone exceeds the output budget
(estimated X tokens > Y cap); split or shorten the line manually` — raised
before any model call).

The state file is deleted after a chapter is assembled into `translated/`. A
state file that fails to parse restarts the chapter from TRANSLATE — one
`[warn] <stem>.state.json unreadable (<reason>) - restarting chapter state`
line, then business as usual.

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
  files with matches are rewritten, atomically, LF. `glossary replace`
  rewrites every chapter BEFORE saving glossary.json, so a run that dies
  mid-loop leaves the glossary still saying the old translation — one
  `[warn] replace incomplete: k/N chapters rewritten; glossary.json not
  updated - re-run the same command to finish` line, then a clean exit 2 —
  and re-running the same command finishes it (already-rewritten chapters
  match zero occurrences and are skipped, so the re-run is idempotent).

`build-epub` writes `export/<slug>.epub` atomically: the book is assembled
at a tmp sibling of the export path and swapped in with a rename (briefly
retried on Windows when a reader holds the file open), so a concurrent
build or reader never observes a half-written epub.

During `translate`/`retry`, `build-epub` also runs automatically after every
chapter reaches `translated`: per-chapter rebuilds are serialized (with a
guaranteed final build at batch end; a stalled build is killed after 360s)
and failures are warnings only (output in `logs/epub-build.log`), so
`export/` always holds a current epub; disable with `auto_build_epub:
false`.

## Prompt templates (`templates/`)

Copied from the skill's `assets/templates/` at `init`; edit freely per project.
A template file missing from the project's `templates/` dir falls back to
the skill's `assets/templates/`, so newly shipped templates work in
existing projects. `migrate` materializes missing templates onto disk
and prompts for each copy that differs from the shipped one —
`templates ~ <name>.md differs from the shipped copy - overwrite it?
[y/N]` (Enter/n keeps the project's copy, y overwrites; `--force`
answers y to all prompts, and non-interactive runs keep differing
copies with a `[warn]`); this template maintenance pass runs on
already-current
projects too, leaving the version stamp untouched. Plain
`{{placeholder}}` substitution. The pipeline
errors out if a template still contains an unknown/leftover `{{...}}`
after filling — typos fail fast.

| Template | Filled for | Placeholders |
|---|---|---|
| `translation.md` | TRANSLATE (per chunk) | `target_lang glossary feedback_section chapter_title chunk_info style background_section source_lines line_count` |
| `glossary_expand.md` | GLOSSARY_EXPAND | `source_lang target_lang glossary source_lines translation_lines max_terms` |
| `faithfulness.md` | FAITH | `source_lang target_lang source_lines translation_lines background_section balance_signals_section` |
| `tn_generate.md` | TN_GENERATE | `source_lang target_lang source_lines translation_lines background_section glossary max_notes` |
| `recap.md` | rolling story recap (post-ASSEMBLE record; pre-translate backfill) | `source_lang target_lang previous_recap chapter_title chapter_text` |
| `glossary_merge.md` | glossary collision merge | `existing_json proposed_json` |
| `glossary_cleanup.md` | balance drift-signal cleanup | `source_lang target_lang term_list sample_lines` |
| `glossary_review.md` | `review glossary` model tier | `source_lang target_lang entries` |
| `notes_review.md` | `review notes` model tier | `source_lang target_lang entries` |
| `style_profile.md` | `--style auto` init / `profile` (legacy) | `source_lang target_lang sample_text` |

`source_lines` / `translation_lines` are substituted as JSON arrays (compact,
`ensure_ascii=False`); `glossary_review.md`'s and `notes_review.md`'s
`entries` as one compact JSON
object per line (one batch of entries per model call). The `glossary` block
renders in the Hy-MT2 trained terminology format — one pure pair per line:
`筑基 translates to "Foundation Establishment"` — with no categories or
definitions in the translation prompt (they live in glossary.json for the
other stages).
`source_lang`/`target_lang` are always full language names. `style` is the
active style guide (project `style.md`, else legacy
`style_profile.style_summary`, else a generic default); `background_section`
renders the trained `[Background Information]` frame (`novel_info.background`,
else legacy `style_profile.background`, else empty; the rolling story recap
from `story_state.json` — the previous chapter's entry — next; plus the
previous chunk's final lines for chunks 2+; the section is empty when no
background or recap is set and the chunk has no predecessor).

**Whole-chapter translation**: only the OUTPUT is constrained. Each translate
call sends `max_tokens = translate_max_output_tokens` (default 8192, the model
card's recommended range); chapters whose EXPECTED output fits the packing
budget below are
translated in ONE call — the model sees the chapter's full context (input is
never limited by this). Longer chapters split by greedy per-line token-budget
packing: each source line costs its CJK chars + other chars/4 + 10 (the
numbered-JSON wrapper), parts close when the next line would exceed
floor(0.8 × the cap) − 256 (the 0.8 headroom absorbs estimate error; the 256
is the per-part JSON overhead; every part takes ≥ 1 line), with style
background and the previous part's final lines included as input context.
Packing is deterministic given (source, config), so part bounds — and with
them the `[Rejected Previous Attempt]` feedback slices — reproduce exactly
across attempts and resumes. A part whose response looks truncated (missing
line indices, or a response cut mid-JSON) retries once at an escalated cap
(min(round(1.5 × cap), the translator provider's `max_tokens`)); other shape
problems retry at the same cap. A single source line whose estimated output
exceeds even the escalated cap fails fast with actionable feedback before any
model call (split or shorten the line by hand); a line over the packing
budget but under the escalated cap is translated as its own part directly at
the escalated cap. Completed parts are persisted per part (state `chunks`,
see Draft artifacts), so a crash mid-TRANSLATE resumes at the next part.
`chunk_info` is empty for single-call chapters.

## Model response schemas

Requested with sglang guided JSON (`response_format`) when available, with
robust extraction as fallback:

- TRANSLATE → `{"title": str, "lines": [{"i": int, "t": str}, ...]}` — the
  numbered-line protocol: input lines arrive as `{"i", "t"}` objects (1-based
  chapter-global index) and each translated line echoes its input `i`.
  Coverage is verified exactly (missing/duplicate/out-of-range indices become
  corrective feedback). Chapters whose body opens with a line identical to the
  frontmatter `chapter_title` have that line stripped once during chapter
  preparation, before TRANSLATE — the title is carried by the frontmatter
  `title` field instead.
- GLOSSARY_EXPAND → `{"terms": [{"source", "variants": [str], "translation", "definition", "category"}]}` —
  a proposal whose source is contained in a known term's source, or contains
  it, with the same translation (nicknames/short forms) is absorbed as a
  variant of the known entry, never a separate entry. Brand-new-term
  proposals then pass a client-side significance gate: each is counted
  across the whole source corpus (all `source/Chapter_*.md` bodies joined)
  and added only at >= `min_term_occurrences` occurrences (default 3; 0
  disables the gate, and an unreadable corpus fails it open) — updates,
  conflict-merges, and variant absorption of entries already in the
  glossary are never gated; the model output schema itself is unchanged.
  Category handling end to end: the model is offered the glossary
  categories minus `unit` in `glossary_expand.md`'s enum (units come
  from catalogues, not proposals); a proposal carrying any KNOWN
  category — `unit` included — passes validation unchanged, while a
  proposal with an UNKNOWN category is coerced to `other` with the
  console line `[glossary] warn unknown category '<cat>' for
  '<source>' - coerced to 'other'` (in-pipeline lines carry the
  `[Chapter_NNNN]` tag prefix like the other stage lines). That
  coercion is proposal-side, not storage-side — the glossary.json
  `category` enum (above) legitimately includes `unit` — and it is a
  different surface from `review glossary`'s unknown-category heuristic
  finding, which reports a stored entry instead of fixing it
- FAITH → `{"verdict": "SUCCESS"|"FAILURE", "reasons": [str]}`
- recap → `{"recap": str}` — the updated running story-so-far recap
  (`templates/recap.md`): at most 120 words, target language, third
  person; recorded once per chapter after ASSEMBLE and read back as the
  next chapter's `{{previous_recap}}`
- TN_GENERATE → `{"notes": [{"line": int, "term": str, "note": str, "category": "cultural"|"idiom"|"wordplay"|"honorific"|"unit"|"other", "threshold": "high"|"low"}]}` —
  the threshold is the model's self-assessed comprehension judgment
  (Hy-MT2's cultural-adaptation pattern); entries marked `"low"` are discarded
  automatically, missing threshold keeps the note. `category` (what kind of
  context the note carries) is OPTIONAL — models may omit it, and an
  unknown/missing value silently defaults to `"other"`; the annotator prompt
  receives the glossary's pinned renderings (`glossary` placeholder) so
  deliberate transliterations can be annotated for what the source term
  literally carries, and asks for severity-ordered entries so the code-side
  `max_notes_per_chapter` truncation keeps the most severe context loss
- style profile → `{"style_summary": str, "background": str}` (--style auto
  only; stored in novel_info.json)
- glossary merge → single entry `{"source", "translation", "definition", "category"}` —
  an unknown `category` in the merged result is coerced to `other`
  exactly like GLOSSARY_EXPAND proposals (see above)
- glossary cleanup → `{"decisions": [{"source": str, "keep": bool, "reason": str}, ...]}` —
  BALANCE's drift-signal judgment (`templates/glossary_cleanup.md`); a
  `keep: false` decision retires the source as mundane (deferred until FAITH
  accepts the translation; the reason falls back to "mundane term"), and
  decisions naming sources outside the signal list — or duplicating one —
  are ignored
- glossary review → `{"findings": [{"source": str, "kind": str, "severity":
  str, "reason": str, "suggestion": str, "action": str}, ...]}` — the
  `review glossary` model tier (`templates/glossary_review.md`); an unknown
  `kind` is read as "other" and an unknown `severity` as "info", findings
  naming a source outside the reviewed batch are dropped, and a finding
  without a usable reason is skipped
- notes review → `{"findings": [{"idx": int, "kind": "restates"|"overexplains"|"wrong"|"misanchored",
  "severity": "warn"|"info", "reason": str, "suggestion": str}, ...]}` — the
  `review notes` model tier (`templates/notes_review.md`); `idx` references
  the entry's `idx` from the batch it was sent with. Stricter than the
  glossary tier (the vocabulary is closed, there is no "other" bucket):
  rows with an unknown `kind` or `severity`, an `idx` outside the reviewed
  batch, or an empty reason are DROPPED with a
  `[notes] warn dropped finding: ...` line, not normalized

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

Catalogue terms are curated for drift risk, mirroring the glossary
qualification (GLOSSARY_EXPAND / glossary review): a term earns a slot only
when its rendering genuinely competes — named cultivation realms and immortal
ranks ("Golden Core" vs "Gold Core"), real organization names ("Beggars' Gang"
vs "Beggar Clan"), archetype proper names, and world-defining set phrases that
fork between transliteration and translation (江湖 "Jianghu" vs "the martial
world"). Generic domain common nouns (灵石 "spirit stone", 修士 "cultivator")
and speaker-dependent address terms (师兄 "Senior Brother") translate by
context and do not belong in a catalogue; the glossary review's `mundane`
exemption for `origin: "seeded"` entries covers exactly this curated set.

Catalogues are split by domain, not just language: `zh` currently ships four
(`zh-cultivation.json`, `zh-wuxia.json`, `zh-modern.json`, `zh-units.json`).
A catalogue for a
new language (ja/ko) or a new domain is just another JSON file with the right
`language` — `init`/`seed` pick up every catalogue matching `source_lang`.

`zh-units.json` seeds the classical measurement units (里 "li", 斤 "catty",
时辰 "shichen", …) as `category: "unit"` entries — the guide-only category:
injected into the translation prompt to pin the transliteration (a
`tn_generate.md` standing exception asks for a conversion note at the
unit's first chapter occurrence), and ignored by the balance checker.
`seed_min_count` applies to units like any other catalogue term, so
rarely-occurring units rarely auto-seed. The
locative 里 (这里/里面) is a different word, so that entry deliberately
carries no 裡/裏 variants — those are the locative's traditional forms.

## Exit codes

Every command exits 0 on success. The non-zero exits:

| Exit | Meaning |
|---|---|
| 1 | `translate` / `retry` — at least one chapter ended `needs-review`; `build-epub` — epubcheck reported errors (the epub failed validation); `review glossary` — warns remain after the run; `review fix` — at least one command failed, or the report was refused as stale; `glossary search` — nothing found; `glossary count` — below the significance threshold; `tn` — failed chapters (annotator call or unreadable chapter) or no eligible chapters in range; `profile` — generation failed |
| 2 | usage or setup error — bad arguments, missing files, corrupt project JSON (one `[FAIL]` line, never a traceback); `build-epub` — the builder subprocess crashed; `ping` — one or more providers unreachable |
| 130 | interrupted at the keyboard (`Ctrl-C`) — chapter state is saved, re-run to resume |

`build-epub` splits its failures: epubcheck failing validation exits 1,
the builder itself crashing exits 2. `ping` exits 2 whenever one or more
providers are unreachable.
