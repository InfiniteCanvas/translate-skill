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
├── source/              CHAPTER_NNNN.md — untouched source chapters
├── draft/               working area per chapter (see Draft artifacts)
├── translated/          finalized chapters, exactly what the epub is built from
├── notes/               translator's-note sidecars (notes/<stem>.json) + dropped-candidate review artifacts (notes/<stem>.dropped.json)
├── covers/cover.jpg     scraped or generated cover
├── templates/           per-project copies of the prompt templates (editable)
├── styles/              optional per-project style presets (add/override .md files)
├── export/              built epubs
├── .git/                git repository (created by `init`, backfilled by migrate v003) — skill-managed, see Git history
├── .gitignore           skill-managed ignore rules for transient/rebuildable paths (see Git history)
└── logs/                two-tier trace (see § Two-tier trace logs)
    ├── run-<run_id>.jsonl      tier 1: this invocation's orchestration timeline
    ├── report.html             project-wide HTML dashboard (see § report.html)
    ├── report.json             the same dashboard's collected data (see § report.html)
    ├── project/
    │   ├── run-<run_id>.jsonl  tier 2: chapter-less model IO (profile, review)
    │   └── index.jsonl         append-only open/close markers for this invocation
    ├── chapters/
    │   └── CHAPTER_NNNN/
    │       ├── run-<run_id>.jsonl   tier 2: this chapter's model IO
    │       ├── index.jsonl          append-only open/close markers
    │       └── report.md            human-readable summary (metadata-only)
    └── epub-build.log       background epub-build output
```

`run_id` is `YYYYMMDD-HHMMSS-<command>-<pid>`, computed once per resolved
project, so every bucket of one run shares it.

Chapter file names must match `CHAPTER_NNNN.md` and nothing else: **exactly 4**
zero-padded ASCII digits, no suffix, EXACT case (the pattern carries no
re.IGNORECASE). One spelling only: no chapter can be written two ways, and
the 3-digit `Chapter_001.md` is rejected; the former letter-suffix spelling for
extras/bonus chapters (`Chapter_0042a.md`) was removed as unused and is now a
reported near-miss. Chapter order = position in the file list sorted
numerically by the parsed number — with fixed padding that is also plain name
order, so `CHAPTER_0999.md` sorts before
`CHAPTER_1000.md`; that order
is written into each source file's frontmatter and into `chapters.json` as
`order` (0-based), and ALL chapter-distance logic (the translation-note gap)
measures distance in `order` units. The manifest is rebuilt by `init` and
`sync`; `sync` re-scans `source/` for added/removed files and preserves
statuses by file name. Both commands print a `[warn]` for each `source/` file
that looks like a chapter but does not match (short padding, a letter suffix,
`.zh`, a missing `CHAPTER_` prefix, non-ASCII digits, a wrong
extension); non-chapter files are never reported and a warning never fails the
command. See `references/ingestion.md`.

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
| `init --force` over an existing repository (history is kept) | `init: reinitialize project`, then `init: reseed after reinitialize` |
| `sync` that changed anything | `sync: rescan source` |
| `sync-config` | never commits — it writes nothing (report-only) |
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
enters the pipeline: `[CHAPTER_NNNN] [warn] <file>: source chapter has no
content - marked needs-review` — the chapter is marked `needs-review`, no
LLM call.

## Overlay layers

A config resolves in **three layers, lowest precedence first**:

| Layer | Where | Written by |
|---|---|---|
| 0 — code defaults | `DEFAULTS` / `PROVIDER_DEFAULTS` in `scripts/lib/config.py` | the skill |
| 1 — machine overlay | `<skill>/config.local.json` (gitignored, optional) | you, once per machine |
| 2 — project | `<project>/config.json` | `init` and you |

A higher layer overwrites a lower one **key-wise**. `load_config()` composes
all three at **read time** (`config.py: load_config`), and layers 0 and 1 are
never written into layer 2 — so a project's `config.json` holds only the keys a
human put there, and editing `config.local.json` changes every project's
behaviour immediately: no `sync-config` run, no migration. Migrations read
layers 0 and 2 only (`migrations/common.py: materialize_config`), so a step
never freezes one machine's endpoint into a project.

**`base_url` is REQUIRED (v016).** Layer 0 ships no endpoint — it used to ship
one machine's private LAN address, which a config that omitted the key silently
inherited and then failed at the first call. Every resolved provider block must
therefore resolve a non-empty `base_url` from layer 1 or layer 2, or the load is
REFUSED with the job named:

```
[FAIL] cannot read config.json: providers.translator resolves no base_url:
set it in the project's config.json or in the skill's config.local.json
```

A job named nowhere inherits the translator's array whole, so a config that
authored an endpoint anywhere passes. Exit 2. See § Exit codes.

A **prompt template** resolves in **two layers, lowest precedence first**:

| Layer | Where | Written by |
|---|---|---|
| 0 — shipped | `<skill>/assets/templates/<name>.md` | the skill |
| 1 — project | `<project>/templates/<name>.md` | you, only when you want an override |

A project file **wins** — with one exception. If its bytes are byte-identical
to a revision the skill has shipped for that name, it is an unmodified
leftover from the era when `init` copied every template into the project, and
the skill's current copy is used instead (`pipeline._load_template`). Hashing
raw bytes is what makes the exception honest: a file you touched differs,
however slightly, and then keeps winning. A missing project file falls through
too, so a newly shipped template works in an old project.

The known-shipped revisions live in `assets/templates/.history.json`:

```json
{
  "translation.md": ["<sha256>", "<sha256>"],
  "consensus.md": ["<sha256>"]
}
```

**One entry per template name, every revision it has ever shipped**, seeded
from git history — not just the current bytes. A list holding only the current
revision would classify every pre-v017 project's identical copy as a
customization and strand it permanently. A maintainer authoring a prompt edit
appends that revision's hash in the same change; `AGENTS.md` requires a
migration for any `.history.json` edit, because the file's correctness depends
on the revisions recorded rather than on the text of any one template. The
file is data owned by the skill, so a project carries no bookkeeping: an
unreadable or malformed `.history.json` classifies nothing, which degrades to
today's behaviour (every project file wins) rather than to "everything is a
leftover".

`style.md` is **not** an overlay. It is a chosen project data shape copied from
`assets/styles/` at `init`, and the preset name is recorded in
`novel_info.json`; there is no honest signal separating "you edited this guide"
from "this is the unmodified preset".

## config.json

The block below is the **resolved** view — what the three layers add up to.
A project's own file holds only what was authored there (layer 2), which for a
project created without `--api-base` is just the languages and the version
stamp; `init` copies no defaults and no overlay values into it.

```jsonc
{
  "source_lang": "zh",
  "target_lang": "en",
  "providers": {
    "translator": [
      { "base_url": "https://api.example.com/v1", "model": null,
        "temperature": 0.7, "top_p": 1.0, "max_tokens": 65536,
        "max_tokens_limit": null,   // optional: the PROVIDER's hard ceiling (e.g. 131072 on Z.AI); bounds every call to this block, including the consensus merge. Omit = no declared limit
        "thinking": false },   // sglang chat_template_kwargs.enable_thinking; false = output budget spent on the answer, not a reasoning chain
      { "base_url": "http://192.168.1.20:8000/v1", "model": "Qwen3-235B-A22B",
        "temperature": 0.7, "top_p": 1.0, "max_tokens": 65536,
        "thinking": false }    // 2nd block = 2nd model: multi-model consensus (see below)
    ],
    "glossary":    [ { "...same block shape, temperature 0.2" } ],
    "reviewer":    [ { "...same block shape, temperature 0.0",
                       // hosted-provider example: any block can call a 3rd-party
                       // OpenAI-compatible endpoint with Bearer auth
                       // "base_url": "https://api.z.ai/api/paas/v4", "model": "glm-5.3",
                       // "api_key_env": "ZAI_API_KEY" },   // or "api_key": "sk-..." inline
                       // optional "extra_body": { "thinking": { "type": "disabled" } }
                       // merges provider-specific params verbatim into the request body
                       // (after the known knobs, before response_format; ping's probe never sends it)
                     } ],
    "annotator":   [ { "...same block shape, temperature 0.2" } ],
    "recap":       [ { "...same block shape, temperature 0.2" } ],   // rolling story-so-far recap generation (one cheap call per chapter; point it at a cheap model)
    "profile":     [ { "...same block shape, temperature 0.3" } ],   // style-profile generation (--style auto / `profile` only)
    "consensus":   [ { "...same block shape, temperature 0.2" } ]    // merges a multi-model job's candidates into the final response (exactly one block)
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
  "translate_max_output_tokens": 65536, // per-call OUTPUT CEILING + packing budget for splitting: parts close past floor(0.8*pack_cap)-256 of per-line estimated cost, where pack_cap = min(this, the smallest providers.translator block's EFFECTIVE cap); input context is never limited. This is a CEILING, not an override — a block's own max_tokens is a hard provider limit the pipeline may lower but never raise, so a block below this value simply wins and this number is not the cap in force. 64k, raised in v009 from 8192 so a reasoning model drawing from the same budget (measured ~10.5k on GLM-5.3's translator's-notes pass) does not truncate the answer. A translator block whose effective cap is below 8192 cannot be packed into at all.
  "style_sample_chapters": 4,    // chapters sampled (at random) for style-profile generation (--style auto only)
  "style_sample_chars": 12000,   // rough source-character budget for the sample (--style auto only)
  "log_orchestration": true,    // tier-1 structural events (see § Two-tier trace logs); false leaves the chapter tier-2 files and both indexes untouched
  "log_llm": true,               // llm_request/llm_response ONLY; result/chunk/feedback and both indexes are unconditional, so the chapter tier-2 file survives
  "log_prompt_bodies": true,     // the prompt/response TEXT; false records prompt_chars / response_chars and keeps finish_reason/usage/elapsed_s/error
  "log_llm_keep_runs": 10,       // retention for logs/ and logs/project/; older runs pruned to the newest N (by mtime)
  "log_chapter_keep_runs": 3,    // retention per chapter directory, independent of every other chapter
  "review_batch_size": 40,       // entries per `review glossary` / `review notes` model review call; `--batch-size` overrides per run
  "review_report_path": "review-report.md", // advisory review report filename, relative to the project dir (written by `review glossary` / `review notes`, read back by `review fix`)
  "version": 16                  // project version (see Migrations) — written by `init` (fresh projects are born current) and `migrate` (stamped after each successfully applied step) ONLY, never merged from DEFAULTS — the raw on-disk value is the source of truth; a config.json without the key is version 0
}
```

- `base_url` includes `/v1` (OpenAI-compatible) and is **required** — see §
  Overlay layers. `model: null` means "ask
  `/models` and use the first model" — resolved at runtime (cached per
  base_url + resolved auth identity, so two jobs sharing a URL with
  different keys keep distinct resolutions), works with any sglang/vLLM
  server.
- `providers.<job>` is an **array of provider blocks** — one block per
  model. A bare block object (no array) is the legacy single-model shape
  and loads unchanged: it normalizes to a one-element array, so pre-v8
  config.json files need no edit. Per-block keys are unchanged (`model`
  stays a scalar string; `null` → the `/models` first id), and every block
  independently carries its base_url, auth, and sampling knobs.
- Each job can point at a **different provider**: keep `translator` on the
  translation model, later point `reviewer` at a stronger model. A
  non-consensus job you omit inherits the `translator` array whole,
  element-wise: each element merges onto that job's own defaults, so
  authored keys win while the job's sampling defaults (temperature et
  al.) still apply.
- **Multi-model consensus**: a job whose array carries more than one block
  fans every prompt out to all its models in parallel (one worker thread
  per model; Ctrl-C cancels pending calls and abandons in-flight ones),
  then ONE call to the `consensus` provider synthesizes the final
  response from the original task prompt plus the labeled candidates,
  under the task's own JSON schema — downstream validators treat it
  exactly like a single-model reply. Console: `[consensus] {job}: {n}
  model(s) - merging results via the consensus provider` announces the
  fan-out (once per process per job/n). Failure policy: **terminal**. A candidate
  that fails prints `[FAIL] consensus: {job} candidate {i}/{n} ({model}) failed:
  {type}: {err}` and stops the run once every failure has been named — a
  chapter built from whichever model happened to answer is not the chapter the
  project configured, and the old survivor path lost the multi-model merge
  silently. A failed consensus call stops it too; it no longer degrades to a
  candidate. Both exit 3 (see § Exit codes), and a provider that calls the
  request itself unrecoverable never gets here at all. A single-block job
  behaves exactly as before: one call, no
  consensus. The `consensus` job itself is special: exactly ONE block —
  when omitted it inherits only the translator's FIRST block (that
  block's authored keys win; the consensus temperature 0.2 applies only
  when the block leaves it unset), and an explicitly authored multi-block
  consensus array is rejected at load. That derivation is a safety net rather
  than a live route — a config that actually fans out must author `consensus`
  (see below), and one that does not fan out never reaches an arbitrator.
- **Per-job arbitrators — `providers.consensus_<job>` (v015).** The arbitrator
  is resolved **per job**, so two fan-out jobs can merge through two different
  models. An optional sibling key `consensus_<job>` overrides the global
  `consensus` block for that one job's merge — `<job>` being any of
  `translator`, `glossary`, `reviewer`, `annotator`, `recap`, `profile`,
  `verify`
  (`consensus` itself is excluded: it is the global arbitrator and never merges
  candidates of its own). A partial block merges KEY-WISE onto the resolved
  global block, so `{"model": "big-arbiter"}` keeps the global's `base_url`,
  `api_key_env` and sampling knobs and overrides only the model — a partial
  override can never quietly re-point a working provider at some other
  endpoint. **An absent `consensus_<job>` resolves to the global block
  unchanged**, so a project that authors none behaves exactly as it did before
  v015. Resolution order, bottom-up: `PROVIDER_DEFAULTS["consensus"]` ← the
  resolved `providers.consensus` ← the authored `providers.consensus_<job>`.
  `consensus_<job>` is shape-normalized at load (a bare block object → a
  one-element array, so it stays visible in the run ledger's `provider_jobs`
  snapshot) but its keys are **not** pre-filled: resolution happens at read
  time, so a hand-built cfg resolves identically to a loaded one. Errors:
  a two-block `consensus_<job>` raises `providers.consensus_<job> must list
  exactly one model (got {n})`; a suffix that names no job raises rather than
  passing through untouched and silently never being read.
- **`consensus` is REQUIRED the moment any job fans out (v015).** A config with
  two or more blocks in any job and no authored `providers.consensus` is
  REFUSED at load (`[FAIL] cannot read config.json: providers.consensus is
  required because N job(s) fan out to more than one model (annotator,
  glossary, profile, recap, reviewer, translator, verify) and no consensus
  provider is set. Add providers.consensus explicitly, or give those jobs a
  single provider block.`, exit 2 — the message names every `CONSENSUS_JOBS`
  member, so its list follows the schema). Inference must not decide where a
  paid merge call goes: with `consensus` unauthored the fallback is derived from
  `providers.translator[0]` — its endpoint, model, temperature, `max_tokens`,
  `max_tokens_limit` and `extra_body` — so in a two-model translator array
  every merge would silently be synthesized by one of the models it is
  judging. Scoped to fan-out: with every job at a single block nothing merges,
  no arbitrator is ever called, and the config loads unchanged.
  - Authorship is read from the **raw** file, never the normalized one —
    `_normalize_providers` materializes `consensus` for every config, so the
    loaded dict cannot tell authored from derived. The fan-out list IS computed
    on the normalized dict, which is why it names every `CONSENSUS_JOBS` member
    even when the file spells out only `translator`.
  - A `consensus_<job>` key does **not** substitute for the global: with one
    authored and no `consensus`, the load is still refused.
  - **The `consensus` <- `translator[0]` derivation is no longer reachable
    through `load_config`** — a config that fans out must author `consensus`,
    and one that does not fan out never reaches an arbitrator. The derivation
    is kept rather than deleted, because `consensus_provider`'s absent-key path
    calls `provider(cfg, "consensus")` and a hand-built cfg bypasses
    `load_config` entirely; deleting it would trade a harmless fallback for a
    `KeyError`. Read it as a safety net, not a routing rule.
  - No lockout: the composed config is validated as it is read, so an overlay
    that *adds* `providers.consensus` satisfies the requirement and repairs the
    project on the next load, with no file to write. `ping` probes each
  in-force arbitrator under the label `consensus(<job>)`, deduplicated against
  the blocks already probed — a project with no per-job key prints exactly the
  same lines it always did. Note that the arbitrator is deliberately NOT part of
  the chapter packing budget (see `max_tokens_limit` below): `provider_max` is
  computed from the translator's own blocks, so a deliberately small
  `consensus_translator` bounds the *merge*, not the chapter.
- **`max_tokens_limit` — the provider's HARD ceiling (v013).** A provider block
  may declare the largest `max_tokens` its endpoint accepts. Absent, it is
  exactly `max_tokens` and nothing changes; present, it is a ceiling on EVERY
  call to that block, **including the consensus synthesis**, which is otherwise
  allowed past its own block's `max_tokens` (see below). The effective cap is
  `min(max_tokens, max_tokens_limit)` and is what both the wire and the packing
  budget use, so a chapter can never be sized for a response the provider will
  truncate. Three consequences worth knowing:
  - It exists because `max_tokens` alone cannot say both "this block is
    deliberately under-provisioned, raise it to merge" and "this provider
    refuses above N". A `consensus` block set to 128000 on Z.AI (real limit
    131072) under a 256000 task cap used to be sent 256000, answered
    `HTTP 400 code 1210 [1,131072]` — which under v014 is an immediate fatal
    rather than a silent quality loss, see § Exit codes.
  - It bounds the `max_tokens` field only. A provider honoring
    `extra_body.max_completion_tokens` — which the MiniMax blocks do — sits
    outside it: that is a different parameter, merged alongside `max_tokens`,
    not instead of it.
  - The consensus job inherits `translator[0]`'s authored keys when it is not
    authored itself, so a limit on the translator's first block also bounds an
    inherited consensus block that may point at a different provider. Set the
    key explicitly on the consensus block when the two differ. Under v015 the
    same holds one level down: a partial `consensus_<job>` inherits the global
    arbitrator's `max_tokens_limit`, so a limit declared only on
    `providers.consensus` also bounds every per-job arbitrator that does not
    override it. A per-job arbitrator that overrides `max_tokens` but not
    `max_tokens_limit` keeps the inherited limit — declare it explicitly there
    too when the provider's real ceiling differs.
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
  and are not validated the same way — `max_tokens_limit` included, where
  absent, null and 0 all mean "no declared limit".
- A `providers.<job>` value that is neither a block object nor an array of
  block objects fails config load (`[FAIL] cannot read config.json: ...`,
  exit 2), with one of four messages — `providers.{job} must be a provider
  block (object) or an array of blocks`, `providers.{job} must not be an
  empty array`, `providers.{job}[{i}] must be an object`, and
  `providers.consensus must list exactly one model (got {n})`. A per-job
  arbitrator `providers.consensus_<job>` raises the same four with its own key
  name substituted (so `providers.consensus_translator must not be an empty
  array`), and additionally raises `providers.consensus_<job> must list exactly
  one model (got {n})` for a two-block arbitrator, or
  `providers.consensus_<job> names no job: expected consensus_<job> where
  <job> is one of ...` for a suffix that is not a real job. The last one is
  deliberate rather than a pass-through: an unrecognized providers key is
  otherwise carried through untouched, so a typo'd suffix would load cleanly,
  be read by nothing, and leave that job quietly merging through the global
  arbitrator.
- A translator `max_tokens` below `translate_max_output_tokens` is a SUPPORTED
  configuration since v012, not a defect: the block's own limit wins. Each
  translator block's `max_tokens` is a hard ceiling the pipeline may lower but
  never raise, and where it is lower the block governs. Chunk packing packs
  against the SMALLEST `max_tokens` across blocks (so no model in the array
  truncates its part), and the once-per-run note
  `[info] config: translation output cap is N, not
  translate_max_output_tokens (M) - the tightest providers.translator block
  caps it there; chapters pack to N` reports the effective cap, with N the
  minimum across blocks. Two `[warn]` lines still fire, because both cost
  output quality rather than merely reporting a choice: a translator block
  below 8192 cannot be packed into at all, and a `reasoning_effort` high/xhigh/
  max block that the ceiling lowers below its own declared `max_tokens` may stop
  converging (reasoning draws from the same budget as the answer).
- Downgrade caveat: skill versions before v8 silently ignore an array
  `providers.<job>` value (the job falls back to its defaults), so moving
  a project back to an older skill after using multi-model arrays means
  restoring the dict shape by hand.

## config.local.json (skill-level overlay, optional)

`novel-translator/config.local.json` — an OPTIONAL, gitignored file beside
`scripts/` in the skill directory, holding any subset of the keys
`config.json` accepts. It exists so per-machine settings (endpoint, model,
auth) are written once instead of per novel. It is not part of any project
and never appears in a project's file tree.

It is **layer 1** of § Overlay layers: read at every config load, deep-merged
onto layer 0 with overlay values winning, then deep-merged under layer 2 — so
the project's own keys always win, and every key the overlay does not mention
keeps the value the layer below resolved (including `version`, which the
overlay may not carry). Nothing is written to the project:

- `init` reads it and reports `[init] [ok] config.local.json: {n} key(s)
  resolve from the skill overlay (not copied into config.json)`. Absent file →
  nothing happens and nothing is printed.
- `sync-config` is **report-only** — it prints what the layers resolve to and
  writes nothing:

  ```
  [ok] config.local.json: 3 key(s) resolve from the overlay (config.json is not modified)
  [ok] max_attempts = 3 (config.local.json)
  [ok] translator https://api.minimax.io/v1 model=MiniMax-M3
  [ok] glossary   https://api.minimax.io/v1 model=MiniMax-M3
  ...
  [ok] layers: code defaults <- config.local.json <- config.json
  ```

  Exit 0 on success or when there is no overlay file
  (`[ok] no local config at <path> - nothing to sync`); exit 2 when the file
  is malformed, when the project has no `config.json`, or when the layers do
  not resolve a usable config.

Malformed input on EITHER side is a clean `[FAIL]` line and exit 2, never a
traceback: an overlay that is not a JSON object, that carries `version`, or
whose `providers` is not an object; and a project `config.json` that is not
a JSON object or whose `providers` is not an object. Both are shape-checked
before the merge, so a refused read leaves every file byte-identical. A
`providers.<job>` block of the wrong shape inside an otherwise well-formed
overlay (an empty array, an array of non-objects, two `consensus` blocks) is
likewise rejected before anything runs. Because the overlay is consulted on
every load, a malformed one fails EVERY command, `init` included, rather
than only `init`/`sync-config` — loud and early beats a silently wrong
endpoint.

That "every command" includes the file-only upkeep commands (`review fix`,
`util replace`, `glossary replace|search|count`), which tolerate exactly one
thing: a project with **no `config.json` at all**. They read chapters and
`glossary.json` and need no provider endpoint, so they fall back to
`DEFAULTS` for the one threshold each wants (`review_report_path`,
`fuzzy_max_distance`, `min_term_occurrences`) and still exit 0/1. A config
that is *present but broken* — malformed JSON, or blocks that resolve no
`base_url` — is not in that exemption: those six commands report `[FAIL]`
and exit 2 like every other command, rather than silently running against
defaults the user never asked for.

`init` resolves the same three layers **before it writes anything**: with no
`--api-base` and no overlay that resolves an endpoint it refuses up front and
the project directory is left exactly as it was found (no `config.json`, no
`scaffold dirs`, no `novel_info.json`, no repository), rather than half-built.
The message names the job and both remedies:

```
[FAIL] init: providers.translator resolves no base_url:
set it in the project's config.json or in the skill's config.local.json
```

`migrate` is the one command that deliberately does **not** require a
resolvable endpoint: no step makes a provider call, and the projects it
exists to repair — the ones v016 strips the retired endpoint out of, and the
ones whose endpoint lives only in the overlay — are exactly the projects
that would otherwise be unable to run the repair. Every other refusal
(no `config.json`, unparseable JSON, a malformed `providers` block) still
stops it before a step runs.

Merge rules (one step of the chain, applied per adjacent layer pair):
objects and scalars merge key-by-key; a `providers.<job>` given as an ARRAY
replaces that job's blocks wholesale, while a single block OBJECT merges into
every block the layer below already has (so `{"model": "x"}` re-points the
model without dropping that endpoint/auth). The single-block rule composes
across three layers as designed: with the overlay supplying `[A, B]` and the
project naming only `{"model": "m"}`, the resolved translator is
`[{A, model}, {B, model}]` — the project's one block spread across BOTH of the
overlay's blocks.

Secrets: an inline `"api_key"` stays in this file (gitignored) and is never
copied into a project's `config.json`, so it cannot reach a project repo; both
entry points still print a `[warn]` naming each one, because the key is on disk
in plaintext. Use `"api_key_env"` to keep it in the environment and out of every
file.

Ready-made overlays for two hosted providers ship beside this section:
`config.local.example.zai.json` (Z.AI/GLM) and
`config.local.example.minimax.json` (MiniMax/M3), with
`config.local.EXAMPLES.md` documenting every provider-block field and the
provider quirks they encode.

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
  { "file": "CHAPTER_0001.md", "number": 1, "order": 0,
    "status": "translated", "title": "山边小村" },
  { "file": "CHAPTER_0002.md", "number": 2, "order": 1,
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
(~6.3s in total) and on exhaustion prints
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
  category 'unit' (guide-only: balance checks skip it)`) — from every
  assignment path: model proposals, glossary-merge responses, `glossary
  set`, and review's in-review `--fix` (apply_fixes) alike. An entry with no `translation` yet
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
--fix` applies to its own fixes. A hand-written `glossary set` command
whose `--category` value is outside the category vocabulary is skipped
the same way, before any subprocess runs (console: `[review fix]
skipped [N]: unknown category '<value>' (must be one of: place, person,
org, skill, technique, level, state, item, honorific, unit, other)
(<command line>)`) — the vocabulary check is glossary-independent, so it
also fires when glossary.json is unreadable. A `- Command:` bullet carrying
`--project` (as `--project X` or `--project=X`) is likewise never run:
the executor prepends its own `--project`, and a hand-added one would
silently retarget the command (console: `[review fix] skipped [N]:
command overrides --project`), counted the same way. Conflicts are keyed
per (field, resolved source), not per verb: `replace` edits the
translation and shares `set --translation`'s key, so the two collide —
two commands writing one field of one entry is the exact pair that
double-applies (the second rewriting every chapter back); a multi-field
`set --translation X --definition Y` occupies BOTH field keys; sources
resolve through glossary.find(), so a command naming a variant and one
naming the canonical source collide; `merge` keys the resolved
(`--keep`, `--remove`) pair — one report legitimately emits two merges
into one keeper — and `retire` keys the resolved source. A later command
conflicting on ANY of a queued command's keys is skipped whole — the
first one queued wins, later ones are skipped (console:
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
  `[notes] warn dropped finding: ...` line. One failed batch is reported and
  skipped, never fatal — **except** a provider call that fails: that is fatal
  everywhere (§ Exit codes), because a review that silently reviewed fewer
  batches than it reports is the same silent downgrade the consensus path had.

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

### [1] warn / restates / CHAPTER_0042.md / 灵根

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
project's copy reads as drifted) (idempotent). `v008` (DESCRIPTION:
`provider arrays + the consensus job (multi-model consensus); ship
consensus.md`) converts the project's `providers.<job>` values to the
array form and materializes the new `consensus` job — no top-level key
is added, removed, or renamed (the schema change lives entirely inside
`providers`), so the conversion rides the established provider
normalization: load_config wraps a legacy single-block dict into a
one-element array, keeps every user-set key verbatim inside its block,
lets an omitted job inherit the project's translator list, and
materializes the consensus entry, reporting the same
provider-blocks-normalized line as v007; the template sync ships the new
`consensus.md` (the synthesis prompt over the original task plus the
labeled candidates) (idempotent). `v009` (DESCRIPTION: `raise the
translation output budget to 64k (translate_max_output_tokens
8192->65536 and provider max_tokens 16384->65536, together)`) rewrites
BOTH numbers together: `translate_max_output_tokens` 8192 -> 65536 and
every `providers.<job>[].max_tokens` still at 16384 -> 65536. They move as a
pair on purpose — `pipeline._escalated_cap` clamps the corrective retry for a
truncated chunk to the smallest translator `max_tokens`, so raising only the
translate cap would make the retry smaller than the attempt it is retrying
and guarantee the same truncation. Unlike v001-v008 this step REWRITES
existing values rather than only adding missing keys, and only where the value
exactly equals the old default (the chain's "untouched default" rule), so a
project on any other number keeps it; a value that was genuinely chosen at
8192 cannot be distinguished from an untouched one and is bumped —
reversible by setting it back, after which `migrate` is a no-op. It reads
and writes the RAW config (not load_config's merged form) so the report names
the numbers that actually moved, and delegates the unchanged template sync
to `common.sync_templates` (idempotent). `v010` (DESCRIPTION: `rename source
chapters to the fixed 4-digit, all-caps canonical form (Chapter_001.md ->
CHAPTER_0001.md), carrying chapters.json, story_state.json and the per-chapter
artifacts`) renames `source/` files to the one form `CHAPTER_RE` accepts —
`CHAPTER_NNNN.md`, exactly 4 digits, exact case — so short padding, non-canonical
case and
the removed letter suffix stop being silently dropped. A rename orphans
everything keyed on the file name, so the step moves
the whole set: the source file, `translated/<file>`, every `draft/<stem>.*` and
`notes/<stem>*` artifact (globbed per stem, so an artifact a later version adds
is carried rather than stranded), `chapters.json`'s `file` field, and
`story_state.json`'s recap keys. Artifacts move BEFORE the manifest is
rewritten, so a failure partway leaves the manifest pointing at names that
still exist rather than the reverse; `story_state.json` is read and written
directly rather than through `lib/story`, whose `load_state` prunes entries
whose stem is absent from the manifest and would discard the recaps instead of
moving them. A target name that already exists is never clobbered: the file is
skipped with a `[warn]` naming the clash. **Extra chapters are deliberately not
renamed** — a retired suffix name like `Chapter_0042a.md` produces a `[warn]`
listing every one and telling the operator to hand the decision to their agent.
With the suffix gone, the mechanical rename would be `Chapter_0042a.md` →
`CHAPTER_0042.md`, which a real chapter may already own: renaming would
clobber it. There is also no mechanical answer to what number an extra
actually wants (is `0042a` chapter 43? a second part?), so the TOC decides.
This step changes no config key and delegates only to
`common.sync_templates` (idempotent: a second run renames nothing and reports
only the still-deferred extras).
`v014` (DESCRIPTION: `a provider failure that exhausts its retries is now fatal
(exit 3) instead of degrading quietly, and Z.AI irrecoverable codes skip the
retry ladder; report provider blocks with no usable credential`) is
**report-only** on v012's reasoning: no value on disk is wrong that was right
before, and no config edit can express "this provider will be down", which is
the failure this policy is about. It adds no key, so `DEFAULTS` is untouched.
What it reports is the one thing observable from the file and the environment
that will now stop a run at its first call: a provider block with no usable
credential — `[FAIL]` when the block names no `api_key` or `api_key_env` at
all, `[warn]` when an `api_key_env` is merely unset in the current shell (common
and usually correct for the shell the operator will actually use; a migration
must not refuse to run over a shell's environment). Jobs omitted from the file
are skipped rather than reported, since they inherit the translator's array at
load time. Idempotent by construction: it reads, reports, writes nothing.
`v013` (DESCRIPTION: `provider max_tokens_limit: a block may declare its
provider's hard output ceiling, which binds the consensus synthesis too; report
a consensus block the merge would exceed`) is **report-only** and rewrites
nothing, on v012's reasoning: no value on disk is wrong that was right before,
and the migration cannot know any provider's real limit — that number lives in
the vendor's docs. It adds no key, so `DEFAULTS` is untouched and there is
nothing to materialize. What it does read is the one condition observable from
the file alone: the synthesis for a multi-model job would send **more than the
consensus block's own declared `max_tokens`** — `max(task cap, block cap)` above
`block cap` — and that block declares no `max_tokens_limit`. That is exactly the
shape that answers `HTTP 400` when the declared number is the provider's
ceiling. The polarity matters: the report is *not* "`translate_max_output_tokens`
exceeds `providers.consensus.max_tokens`", because a block below the ceiling is
an explicitly SUPPORTED configuration (see the consensus bullet above) and
warning on it would fire on healthy projects. It is an `[info]`, names the
number that will go on the wire and the declared one beside it, says the shape
is supported, and says nothing was rewritten. A block that already declares
`max_tokens_limit` reports `[ok]` and is left alone, and so does a single-block
translator, which never fans out and therefore never calls a consensus provider.
`v016` (DESCRIPTION: `v2 config layering: strip the defaults and overlay
values materialized into config.json, and the retired default endpoint`) is the
migration for § Overlay layers. Every project written before it was written the
other way — `init` copied `DEFAULTS` in, and `init`/`sync-config` copied the
overlay's endpoint, model and auth in — so each `config.json` carries a frozen
snapshot of both layers. (No command copies the overlay into a project any
more, and the helper that used to do it has been removed rather than left
callable.) v016 unwrites that snapshot, and it does so in three classes:

1. **The retired `base_url`.** A provider block whose `base_url` byte-equals the
   retired private LAN address (`config.RETIRED_BASE_URL`) is stripped and
   reported: `[warn] providers.translator.base_url was the retired default
   http://100.85.218.125:8888/v1; set it in config.local.json or the project`.
   This is the one key that cannot follow v009's value-identity rule. v009's
   sentinel is safe because the value it strips still resolves through layer 0;
   `base_url` is the one field layer 0 no longer has, so stripping it on
   value-identity would leave the job with NO endpoint at all. It is stripped
   only where it still carries the retired address — a project's own (or its
   overlay's) endpoint keeps it, because that value was authored and layer 0
   cannot supply a replacement. A project on a machine with no `config.local.json`
   is left with no endpoint and is refused at the next load, with the job named
   (§ Overlay layers); that is the intended, loud failure.
2. **Default-valued provider keys**, stripped per job against THAT job's
   `PROVIDER_DEFAULTS` block — `_with_defaults` fills a job-specific key set
   (translator has `top_p`, the rest do not), so a flat comparison would either
   miss a key or strip one the job never inherited.
3. **Top-level keys** whose value equals the `DEFAULTS` value.

Classes 2 and 3 can only lose a value that resolves identically through layer 0,
so they are safe — with one consequence worth stating: when a project's frozen
default and the machine's overlay disagree, stripping hands the key to the
overlay, which is the point. A project that had never touched the key should
follow the machine, not freeze layer 1's old snapshot. **A value the project
actually chose is never touched**, and v016 never reads `config.local.json`: a
migration that folded layer 1 into a project file would make the same project
migrate differently on two machines. Every stripped key is named in the report.
Idempotent: a second run matches nothing.

Note the round-trip this creates in a single migrate run: a below-v016 project
runs v006–v015 (which materialize) and then v016 (which strips), so the net
result is the v2 shape. A below-v016 project whose blocks still carry the retired
LAN endpoint loads fine through those earlier steps — the value is non-empty —
and is corrected by v016 itself.

`v017` (DESCRIPTION: `v2 template layering: delete project template files that are
unmodified shipped copies; keep every customization`) is the migration for § Overlay
layers' **template** half. Every project written before it was written the other way:
`init` copied all eleven shipped templates into `templates/`, so each project carried
a snapshot of the skill's prompts as they stood the day it was scaffolded. Those
copies are exactly what the new resolution rule already ignores — a file whose sha256
is listed for that name in `assets/templates/.history.json` is an unmodified
leftover, and the skill's current copy is what runs — so v017 deletes them and makes
the project's real state visible: whatever remains is an author-written customization,
and it keeps winning.

The distinction is **byte identity, not judgement about content**, so there is nothing
to consent to: `force` and `confirm` are accepted and unused, and a deletion here can
never be the loss of an edit. Each deletion is reported
(`[ok] templates - <name>.md (unmodified shipped copy; the skill's template is used
from now on)`); customizations are left alone and silent, which is what makes the
step idempotent — a second run matches nothing and reports nothing. A project with no
`.history.json` visible to the migration reports one `[warn]` and keeps everything,
the conservative direction: an unknown revision must not authorize a delete.

Round-trip note, same shape as v016's: a below-v017 project's earlier steps still call
`sync_templates()`, which materializes any template it is missing and refreshes drifted
ones. Those materialized copies are byte-identical to the shipped file by construction,
so v017 deletes exactly the ones that step wrote and keeps every customization —
including one `--force` did not overwrite.

`v018` (DESCRIPTION: `record the `verify` provider job added for `translate verify`
(report-only: it inherits the translator's blocks until authored; config.json is not
rewritten, and the job's two prompt templates are synced in)`) records
`providers.verify`, the job `translate verify`'s stage 2 spends calls on. A job
named nowhere **inherits the translator's whole array** (`_normalize_providers`), so
the new job resolves to working blocks without any project file changing — which is
why the step rewrites no **config**: materializing it would be exactly the pre-v016
behaviour the overlay chain removed (pinning one machine's endpoint into every
project it touched, where it would then out-rank a later edit to
`config.local.json`). It follows v012's report-only precedent rather than v007's:
there is no old-default sentinel to match on, because unlike v012 even a threshold's
MEANING did not shift — `verify` simply resolves an array it did not resolve before.
The report names what the job resolves to, from the **raw** project file (a migration
reads layers 0 and 2 only, so a line about "the endpoint it will use" would describe
one machine's layering):

```
[ok] config: providers.verify is a new job (v018) - it inherits the translator's
provider blocks until you author it; set providers.verify to point the whole-book
drift scan at its own models, or providers.consensus_verify to arbitrate its
fan-out differently.
```

A project that had already authored `providers.verify` gets a line saying so and
is otherwise untouched.

What the step **does** write is templates. It calls `sync_templates`, which copies the
two prompts the job needs — `glossary_verify.md` for stage 2 and
`consensus_verify.md` for its fan-out — into a project that does not have them, so
`verify` is runnable immediately. `config.json` is never written, but the step is not
"writes nothing": `force`/`confirm` are unused *by the config half* and passed
through to `sync_templates`, which does have an overwrite to consent to. Idempotent
by construction — `sync_templates` is silent on a project already carrying both
templates, so a second call reports the same line and writes nothing further.

`v015` (DESCRIPTION: `a fan-out job can name its own arbitrator:
providers.consensus_<job> overrides providers.consensus for that job's merge;
report which arbitrator each fan-out job resolves to`) is **report-only** and
rewrites nothing, on v012's and v013's reasoning: no value on disk is wrong that
was right before, and the migration cannot know which model anyone wants to
merge with — that choice IS what this step enables. It adds no key to `DEFAULTS`
or `PROVIDER_DEFAULTS`, so there is nothing to materialize; the change lives
entirely inside `providers`, like v008's, and is authoritative by v008's
precedent. Materializing the new keys was explicitly rejected: a config would
carry a dead block per job duplicating the global arbitrator's `base_url` and
auth,
and changing the global later would leave stale copies silently winning for
the jobs that had them. What it reports is the one fact the operator cannot see
without reading the file — which arbitrator each fan-out job will merge through,
`providers.consensus_<job>` where authored and `providers.consensus` otherwise —
split by severity, and the split is the point. A config that **fans out without
an authored `consensus`** is REFUSED at load from v015, so that case is a
`[FAIL]`: it predicts a run that will not start, and an `[info]` or `[ok]` would
understate a hard stop. It names every job that fans out, offers both remedies,
and **does not rewrite** — auto-writing a `consensus` block from `translator[0]`
would re-create on disk, silently and with nothing to explain it, the exact
inference the rule forbids; that number is the operator's choice to make. Every
other case stays advisory `[ok]`/`[info]`.
Resolving that map requires honouring the inheritance a real load would apply:
a job omitted from the file inherits the translator's WHOLE array, so it fans
out too even though the file never names it, and a job with one block never
merges at all. The set of jobs it walks is read from `config.CONSENSUS_JOBS`
rather than frozen in the script, because every line this step prints is a claim
about what will happen when the project runs with the code that ships today, and
a frozen list would under-report a job added later (v018 added `verify`). v013 did not need this, because it only ever examined the
translator. v013 itself is left reading the raw `providers.consensus`: the chain
is version-stamped and never re-runs, so a config sitting at v013 cannot contain
a v015-era key unless hand-written before upgrading, and v015's own report is
the authoritative one. v015 also re-aims v013's `max_tokens_limit` check at the
arbitrator that will actually be used rather than the global one, which a
dedicated key may have displaced. Idempotent by construction: it reads, reports,
writes nothing.
`v012` (DESCRIPTION: `decouple translate_max_output_tokens from provider
max_tokens (the key is now a ceiling a block can lower, not an override; report
mismatched translator pairs)`) is **report-only** and rewrites nothing. It reads
the raw config and, where `translate_max_output_tokens` exceeds the tightest
`providers.translator` block, reports that the block now wins as a hard ceiling
and chapters pack to it instead — naming both numbers, the new per-part budget,
and the fact that a chapter already mid-translation will re-pack and restart. A
block below 8192 gets its own line instead, since chapters cannot be packed into
it at all. This step's departure from the rest of the chain is deliberate: v009
could rewrite because it knew the exact old default (16384 / 8192) and anything
else was a user choice. A resolution-rule change has no such sentinel — neither
number is wrong, they just no longer mean the same thing, and either side is a
legitimate configuration — so rewriting a user's chosen numbers because their
*relationship* changed would be exactly the silent edit v009's own docstring
warns against. It is idempotent by construction: it reads, reports, writes
nothing.
`v011` (DESCRIPTION: `two-tier log layout (new log_orchestration,
log_prompt_bodies, log_chapter_keep_runs; log_llm_keep_runs 5->10)`) folds
`config.DEFAULTS` in for the three NEW keys, and rewrites
`log_llm_keep_runs` 5 → 10 in the **raw** config, not the merged form.
That rewrite is not optional polish: `materialize_config` writes the entire
merged config on every `migrate`, so with the old default at 5
essentially every existing project already carries an explicit `5`, and
delegating to the `materialize_config` helper rather than rewriting the raw
file would preserve it — delivering the retention bump to
new projects only. The same ambiguity `v009` accepts applies to this number:
a user who genuinely wanted exactly 5 cannot be told apart from one who
never touched it, and their 5 is bumped; it is reversible by setting it back,
after which `migrate` is a no-op. Same trade-off as `v009`: once written, the
new value is an explicit literal that no longer tracks `DEFAULTS`.
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
    "CHAPTER_0042": {
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
chapter's own entry (console: `[CHAPTER_NNNN] [init] recap`); the next
chapter's TRANSLATE / FAITH / TN_GENERATE prompts receive it inside their
`[Background Information]` frame, as `Story so far (auto-generated recap
of the preceding chapters):` followed by the text. When the predecessor's
entry is missing — chapters translated before the feature existed, or a
crash that landed between ASSEMBLE and the record — it is backfilled with
exactly ONE extra recap call before translation starts (console:
`[CHAPTER_NNNN] [init] recap (backfill CHAPTER_PPPP.md)`), anchored on the
nearest EARLIER existing entry, never a recursive chain. Failures are
advisory and never fail or gate a chapter —
`[CHAPTER_NNNN] [warn] recap backfill failed for <prev>: <reason>` /
`[CHAPTER_NNNN] [warn] recap generation failed for <file>: <reason>` — the
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
  "chapter": "CHAPTER_0042.md",
  "updated_at": "2026-09-06T12:00:00+00:00",
  "notes": [
    { "line": 17, "term": "筑基", "category": "cultural",
      "note": "Foundation Establishment (筑基) is the second realm of cultivation.",
      "anchor": "His breakthrough settled at dawn, and the whole courtyard" }
  ]
}
```

One JSON sidecar per translated chapter (`notes/CHAPTER_0042.json` for
`translated/CHAPTER_0042.md`), holding the translator's notes the epub
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
sidecar (absent = no notes).

The `notes` array is in **reading order** — ascending by `line`, ties keeping
the annotator's relative order. That is the list the epub numbers its
footnotes from, so the reader meets `[1]` on the earliest annotated line; a
note the annotator ranked first but that sits on line 4 still prints as `[4]`.
The severity order the annotator returns is a selection input only: it decides
WHICH notes survive `max_notes_per_chapter`, never how the survivors are
ordered — the sort runs after the cap, not before it. `load_notes` re-sorts on
read as well, so a sidecar written before this ordering still builds with
line-ordered footnotes; the file itself is rewritten in reading order the next
time that chapter is translated or `tn`-re-checked. Entries with no usable
`line` (reachable only by hand-editing) sort to the end of the array and are
dropped by the builder's own anchor check with a `[warn]`.

`updated_at` is managed by the tool. A malformed sidecar is treated as "no
notes" — the epub build never crashes on one — announced by
`[warn] <stem>.json unreadable (<reason>) - treating as no notes`.

## notes/<stem>.dropped.json (dropped-candidates review artifact)

```jsonc
{
  "chapter": "CHAPTER_0042.md",
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
noted elsewhere / suppressed on purpose). Unlike the sidecar's `notes`
array, this one is NOT in reading order: it is grouped by `reason` as the
gates fired, because its job is auditing the gates, not reading alongside
the text.

An `overflow`-dropped term never consumes the gap window: its
`tn_history.json` entry is rolled
back (restored to its pre-call value, or removed when this chapter
introduced it), so a later chapter inside the window can still annotate
it. **Review artifact only — the
epub builder does not read it.** The matching TN_DEDUP console line,
verbatim:

```
[CHAPTER_0042] [ok] notes: 7 kept (cultural 3, idiom 2, wordplay 1, other 1); 4 dropped (3 low-confidence, 1 overflow) -> notes/CHAPTER_0042.dropped.json
```

Categories count non-zero entries in the fixed order
`cultural, idiom, wordplay, honorific, unit, other` (all-"other" keeps
show `other K`); a zero-keep chapter prints `[CHAPTER_0042] [ok] notes: 0
kept` with no parenthetical, and a zero-drop chapter ends the line at the
kept clause (no dropped segment, no file pointer).

## Two-tier trace logs (`logs/`)

Every event belongs to **exactly one** tier, and the tiers never mix.

**Tier 1 — orchestration** (`logs/run-<run_id>.jsonl`). What happened in this
run: `run_start`, `chapter_start`, `stage` (`TRANSLATE`, `VALIDATE`, … each
with `phase: begin|end` and `elapsed_s`), `gate` (the verdict plus its
**complete** reason list), `degraded`, `fatal`, `attempt`, `attempt_failed`,
`balance_advisory`, `glossary_cleanup`, and one `llm_call` metadata summary
per model call. **Tier 1 never carries a prompt or a response** — `llm_call`
holds `model`, `usage`, `finish_reason`, `elapsed_s` and `error` only.

**Tier 2 — model IO, per chapter**
(`logs/chapters/<stem>/run-<run_id>.jsonl`). What the models were given and
said for one chapter: `llm_request`, `llm_response`, plus the pipeline's own
reading of individual model outputs — `chunk` (per-part bounds and outcome),
`result` (the verdict it accepted, the terms it proposed, the notes it kept),
and `feedback` (the complete accumulated reason history).

A tier-2 event with **no** chapter lands in the project bucket,
`logs/project/run-<run_id>.jsonl` — that is where `profile` and the `review`
passes go. The chapter bucket is keyed on the chapter file's **stem**, which
is exactly the existing `draft/` / `notes/` artifact key. No sanitizing:
every stem reaching the logger came from a `CHAPTER_RE` match, so it is
already filesystem-safe.

Any event name not in the tier-2 set defaults to **tier 1**, so an unknown or
misspelled name can never leak model text into a chapter's file.

### `index.jsonl`, `report.md`, `report.html` and `report.json`

`index.jsonl` (one per bucket) is **append-only and never pruned**, and is
written unconditionally of both gates: an `open` line with no matching `close`
is the signal that an invocation died. Retention deliberately does not touch
it — being the oldest file by mtime, "keep the newest N" would delete exactly
the file that records the history.

`report.md` (one per chapter) is the human-readable face of those logs:
header, stage timeline, gate verdicts, and the call table. `chapter_end`
writes a **metadata-only** report bounded to the current invocation;
`translate logs <chapter> --report [--io]` regenerates it across every
retained run, and `--io` additionally wraps each call that actually carried a
body in a `<details>` block. Model output is HTML-escaped on the way in, so a
reply containing `</details>` or a fenced block cannot corrupt the structure.

`report.html` (one per **project**, at `logs/report.html`) is the same idea at
project scale: every retained log assembled into one self-contained page. Two
writers — `_run_end` refreshes it after every `translate`/`retry`/`tn`/
`review`/`profile`/`verify` run, and `translate logs --html` writes it on
demand; the
latter is the path that matters after an interrupt, since `main()` hard-exits
without reaching `_run_end`. Either writer also emits `logs/report.json`, the
same run's collected data.

Its spine is the chapter `index.jsonl` close lines, **not** tier 1, because
`index.jsonl` is never pruned while tier 1 keeps only `log_llm_keep_runs`
runs. Tier 1 is therefore a **windowed enrichment**: it adds stage spans and
gate verdicts for the runs still inside the retention window, and it is the
only source of **spend outside any chapter**.

The per-call ledger, the per-model split and every call-derived total are read
from **tier 2** — the chapter's own `logs/chapters/<stem>/run-<run_id>.jsonl`.
Tier 2 is the better source, not merely a fallback: an `llm_response` line
carries everything tier 1's `llm_call` summary does (`job`, `model`,
`candidate`/`candidates`, `consensus_for`, `usage`, `elapsed_s`,
`finish_reason`, `error`) plus `call_id` and `url`, and the bucket is already
chapter-scoped so nothing needs filtering. Tier 1's rows are used only for a
chapter with no tier-2 call record — i.e. one written while `log_llm` was off,
the one thing that gate removes. Every total is labelled with the source it
came from, so the page cannot silently change meaning.

**One run per chapter, and it is the one the ledger bar reports.** The call
ledger reads the run that `index.jsonl`'s last `open` line names — the same run
whose close line supplies every chapter figure. This matters: with
`log_chapter_keep_runs: 3` a *closed* older run's file coexists with a newer
run's file, so preferring the last `close` line picks the **older** run and puts
one run's bar beside another run's calls. When that run has no body the ledger
falls back to the newest file on disk, and the row says which basis it used.

`llm_request` and `llm_response` are joined on `call_id`. Both sides are stored
and the ordering is what gets de-duplicated — de-duplicating per-side instead
emits every paired call **twice**, and de-duplicating the store drops the
response so every call reads as unpaired with no body. A call present on one
side only (a run killed mid-flight) is kept and flagged `unpaired`, never
discarded. `result` and `chunk` rows carry no `call_id` and are shown in their
own table as stage events, so the ledger holds only calls and nothing is lost.

**Response shape is decided by the parsed key set, never by `job` or
`consensus_for`.** One job name drives several schemas — `glossary` alone runs
expand (`{terms}`), merge (a flat
`{source, translation, definition, category}` object) and cleanup
(`{decisions}`) — and `consensus_for` records only the job name, so it cannot
say which template ran. Dispatch therefore matches the key set against a
registry and falls back to a **generic renderer** (object → key/value list,
array of objects → table over the union of keys, array of scalars → list,
scalar/null → text). The registry is data, so a schema added later degrades to
"readable" rather than to raw JSON. `json.loads` is tried on the raw body and
then on a ```` ```json ````-stripped copy, because a fenced response is real and
`json.loads` rejects it.

**Fan-out width is read, never assumed.** `providers.<job>` is an array of
blocks, so the number of candidates is whatever the project configured. The
side-by-side source view is therefore N candidate columns plus one consensus
column, each headed by the model name from that call's own record — because
candidate `1` is a *different model* for a different job, and on a real project
the reviewer's order is the reverse of the translator's. Source lines are
recovered from the translator prompt by a **balanced scan that respects string
literals and escapes**, never a regex: novel prose contains `[`, `]`, `{` and
`}` inside the line strings, which desyncs any bracket-counting pattern (the
obvious one fails on 20/20 real prompts). The scan is marker-free, so moving a
heading in `assets/templates/translation.md` cannot silently break it.

Every absence is reported rather than rendered as zero, because a plausible
wrong total is the worst thing a diagnostic can do:

- the orchestration tier missing (stage spans and gate verdicts only — the
  per-call view survives, but **spend outside any chapter** — `profile` /
  `review` / `tn` record their calls *only* in tier 1's `run_end`, because the
  project index close line carries `outcome` alone — does not);
- an `epub-build.log` whose final block is half-written by a live build child;
- a bucket whose chapter is absent from `chapters.json`, or whose stem predates
  the `CHAPTER_NNNN` naming;
- a run with no body file, which falls back to the newest file on disk.

Stage spans are keyed on `(stage, attempt)` and **summed across attempts**,
which deliberately differs from `report.md`'s stage table — that one keys on
stage name and so shows only the final attempt. The bar must account for every
second the run spent.

The page makes **no network requests**: no CDN, no webfont, no absolute URL, no
`fetch`/`XMLHttpRequest`/`<iframe>`/`<link>`. Bodies are **always** embedded and
there is **no cap** — the removed 200 KB cap was pure loss, since embedding
every body costs ~36 ms against a 981 s chapter, while the cap dropped 84 of 131
bodies on a real project and, because it accumulated in ascending file order,
dropped the *oldest* runs first. There is no second mode: `--html-io` is gone.

All model text travels in **one** `<script type="application/json">` blob and is
read only through `JSON.parse`; the renderer never puts a log-derived value into
`innerHTML`. The blob is encoded as
`json.dumps(records, ensure_ascii=False).replace("<", "\\u003c")` and **must not
be `html.escape`d**: `<script>` is a *rawtext* element, so the HTML parser never
decodes character references inside it, `textContent` hands `&quot;` back
literally, and `JSON.parse` fails at position 1. Since every response body
contains a quote, that breaks every record on the page. `<` is the only
character that can terminate a script element and `<` is a valid JSON string
escape, so the substitution is lossless — a body holding a literal closing tag
round-trips byte-identical. Note that Python's `html.unescape` is **not** a
faithful stand-in for the browser here: simulating with it passes green for the
broken encoding, which is how the defect survives review.

#### `logs/report.json` (the dashboard's data contract)

One `collect()` feeds both artifacts, so they cannot disagree. The JSON is
written on **every** dashboard run — both writers, the automatic `_run_end`
refresh and `translate logs --html` — with no flag to suppress it, and it is the
shape `logdashboard.collect()` returns, so the page is a *view* of the file
rather than a separate interpretation of the logs. It is derived and lives
under the gitignored `logs/`: delete it and the next dashboard run rebuilds it.

Top level, exactly these keys:

| key | type | what it holds |
|---|---|---|
| `meta` | object | project title/config plus `generated_at` and `project_dir` |
| `chapters` | array | one object per chapter bucket, exactly these keys: `attempts`, `call_source`, `calls`, `calls_detail`, `closed`, `degraded`, `elapsed_s`, `epub`, `gates`, `in_manifest`, `number`, `outcome`, `run_file`, `runs`, `stage_spans`, `stages`, `stem`, `tokens` |
| `runs` | array | project-bucket runs with no chapter of their own |
| `tokens_by_job`, `tokens_by_model`, `calls_by_job` | object | the roll-ups the page's bars and roster read |
| `total_calls`, `total_tokens`, `bodies_available`, `chapterless_tokens` | integer | page totals, each with its source named below |
| `token_source`, `call_source` | string | which tier the totals came from — never silently changed |
| `elapsed_total`, `elapsed_max` | number \| null | wall-clock, `null` when nothing was measured |
| `outcomes` | object | outcome → chapter count |
| `unclosed` | array | `"<stem> / <run_id>"` for every open with no close |
| `gaps` | array | every absence the page declares rather than rendering as zero |
| `calls` | array | the call ledger: one record per model call, each with `call_id`, `run_id`, `chapter`, `job`, `model`, `url`, `candidate`, `candidates`, `consensus_for`, `ts`, `elapsed_s`, `finish_reason`, `error`, `prompt_tok`, `completion_tok`, `reasoning_tok`, `params`, `prompt`, `body`, `parsed`, `shape`, `paired`, `source_lines`. The provider's `usage` object is **not** carried through — the collector reads it and splits it into `prompt_tok`, `completion_tok` and `reasoning_tok` (`null` when the provider reports no reasoning detail), so every token total on the page is an int it can sum |
| `unpaired_events` | array | `result`/`chunk` rows, which carry no `call_id` |
| `has_tier1` | boolean | whether the orchestration tier was available |

It is serialised with `ensure_ascii=False, indent=2`, `default=str` for the
handful of non-JSON values, and `newline="\n"` so the bytes are identical on
every platform and a diff between two runs is meaningful. A write that fails
prints one `[warn] html dashboard data not written` and **does not** fail the
page: the page is what the caller reports on, so a missing derived file must
never turn a good page into "not written".

The page's stylesheet and script are real files in the skill's
`assets/dashboard/` (`report.css`, `report.js`) and are **inlined** by
`render()`. That is the whole point of the split — a linter or formatter can
see them — and it is why the output contract is unchanged: there is still no
`<link>` and no `<script src>`, so one `report.html` remains portable enough to
archive or email by itself. `assets/dashboard/` is **not** `assets/templates/`
and is not part of the `.history.json` template-customisation contract, so
neither the migration rule nor the template-hash invariant applies to it.

**A missing `assets/dashboard/` produces neither artifact.** `render()` reads
both asset files before anything is written, so an install that shipped without
that directory fails the page *and* the data together rather than emitting a
`report.json` no page will ever explain. Each writer — the automatic
`_run_end` refresh and `translate logs --html` — prints exactly one

```
[warn] html dashboard not written: FileNotFoundError: [Errno 2] No such file or directory: '<skill>/assets/dashboard/report.css'
```

`write_dashboard` returns `None`, `refresh` raises nothing, and the calling
command's exit code is untouched: a dashboard is a diagnostic, so losing it may
never fail a translation run. Both writers read the assets first, so an
already-good `report.html` on disk is left byte-identical rather than truncated.
The warning names the exact path, which is the whole diagnosis — it is a
broken install, not a corrupt project; re-copying the skill directory restores
the page on the next run.

### Gates

| key | default | what `false` removes |
|---|---|---|
| `log_orchestration` | `true` | the tier-1 run file only |
| `log_llm` | `true` | `llm_request` / `llm_response` only — `result`, `chunk` and `feedback` are unconditional |
| `log_prompt_bodies` | `true` | the `prompt` / `response` strings, replaced by `prompt_chars` / `response_chars` |

The per-call counters are incremented **before** gating, so `chapter_end` and
`run_end` still report real call and token totals with either switch off.
Flags are resolved once per invocation, not per event.

Pre-v11 `llm-*.jsonl` files are never listed, never matched and never pruned;
they are simply left alone.

## `verify.jsonl` (whole-book glossary-drift scan)

`translate verify` writes its own report at `logs/verify.jsonl` (`--out PATH`
overrides it; a relative path resolves against the project directory). It is a
**result artifact**, not a trace log: it is rewritten whole on every run, is not
part of the two-tier scheme above, and is never indexed or pruned. It is
appended to no index and read by no other command.

Every line carries a `kind`, because the file holds two grains: `"chapter"` (one per
chapter **examined** — a chapter that has both `source/` and `translated/`, in
chapter order, clean chapters included with empty `terms` and `findings`, so the file
is the complete result set a later visualization is built from, not only its
failures) and `"contamination"` (one per glossary entry the model confirmed).

```json
{"kind": "chapter", "chapter": "CHAPTER_0007.md",
 "terms":   [{"source": "天道", "translation": "Heavenly Dao",
              "src_count": 8, "tgt_count": 1, "floor": 2}],
 "findings": [{"source": "天道", "pinned": "Heavenly Dao",
               "chapter": "CHAPTER_0007.md",
               "source_count": 8, "rendered_count": 1, "floor": 2,
               "mistranslates": true, "reason": "..."}],
 "error": null}
{"kind": "contamination", "source": "天道", "pinned": "Heavenly Dao",
 "chapters": ["CHAPTER_0007.md", "CHAPTER_0012.md"], "reason": "..."}
```

| Key | Meaning |
|---|---|
| `kind` | `"chapter"` or `"contamination"` — the record's grain |
| `chapter` | the chapter file name |
| `terms` | **stage 1** — the under-used glossary terms the deterministic scan flagged on this chapter, one per row, with the counts it judged by (`floor` = `max(1, ceil(src_count × min_term_coverage))`) |
| `findings` | **stage 2** — one verdict per `(term, chapter)` pair offered to the model, `mistranslates` carrying the verdict and `chapter` naming which chapter it is about. Present only for a chapter stage 1 flagged; a `(term, chapter)` the model omitted appears in neither list |
| `error` | the failure that stopped this chapter's stage 2, or `null` |
| `chapters` | (contamination records) every chapter the term was confirmed in |

`terms` and `findings` answer different questions and neither substitutes for
the other: `terms` says the count looked low, `findings` says a model judged
the sense. A chapter whose `terms` is non-empty and whose findings are all
`mistranslates: false` was scanned and came back clean.

### How stage 2 is batched

Stage 2 batches **by term across the whole book**, not by chapter: glossary drift is
"words meaning different things in different contexts", and spotting that needs one
term's usages laid against each other — a per-chapter call structurally cannot see a
chapter that renders a term faithfully contradict one that does not, and a recurring
name would otherwise pay the rubric once per chapter. One call carries one term plus
**contexts**: each is the ±2-line window (`CONTEXT_WINDOW`) around one line the term
occurs on, carrying the source lines and the translation lines at the **same 1-based
line numbers**, plus that chapter's counts. Index N in the source is index N in the
translation — the line-indexed protocol VALIDATE enforces — so a chapter whose
translation line count differs from its source's is reported in `error` and left
unchecked rather than paired by guesswork. Both ends of the translation are read the
way `read_chapter` reads a source (blank lines trimmed), because those trimmed source
lines are what "line N" means to VALIDATE too.

A term's contexts are sent in batches of at most `MAX_CONTEXTS_PER_CALL` (8), and a
term with more is split into **follow-up calls until every context is covered**: a
dropped context is exactly where a contaminating one hides, so the cap is paid for
with more calls rather than a silent gap. The call count is printed by the command
(`across N call(s)`) and recorded as `calls` in the run event. A `(term, chapter)`
whose contexts span batches still yields ONE finding — a batch boundary is a payload
limit, not a second opinion. A verdict naming a term or chapter that was not offered
is dropped, and a `(term, chapter)` the model omits is never confirmed.

A confirmed term flags its chapter `needs-review` **and** emits a `contamination`
record naming every chapter it was confirmed in, with a console line naming the term
and those chapters. `glossary.json` is **never** written: it feeds the translator's
pinned-terms list, so a flag stored there would change translation behaviour, which
is a separate decision.

### The automatic cascade check

The same two stages also run **without** the `verify` command, after a
glossary-changing operation (`glossary replace | set | merge | retire`, and
`review fix` for the whole batch). It reuses stage 1 and stage 2 unchanged and
differs only in **scope**: `verify_project(chapters=...)` restricts the scan to
the chapters the edited term actually occurs in, decided before any call is
made, so an edit cannot pay for chapters it could not have reached. The scope is
computed with `glossary.count_in_text` against each chapter's source — the same
matcher the balance advisory and `glossary count` use.

It writes exactly two things: a confirmed chapter's status in `chapters.json`,
and its `verify_cascade` trace event. The rest of the contract is what it does
**not** write, and that is worth stating precisely:

- it **does** flip a confirmed chapter to `needs-review`, through the same
  `project.set_status` path the explicit `verify` command uses and behind the
  same guard that saves the manifest only when something was confirmed. A
  cascade is drift the manual command would have found anyway, and
  `needs-review` is what makes a chapter eligible for `retry`;
- it does **not** write `logs/verify.jsonl`. That file belongs to the manual
  command: `verify_project()` returns records and writes nothing, and only
  `cmd_verify` renders them into the whole-book result set defined above — one
  line per chapter examined. A scoped cascade therefore has no path to the
  file, and reports to the console and the trace event instead;
- it does **not** write `glossary.json`, for the reason above.

`--strict` governs the originating command's **exit code only**: it changes
whether a confirmed cascade exits 1 or 0 and never whether the chapter is
flipped. `--no-verify` opts out of both the scan and the flip.

The automatic run is recorded as a `verify_cascade` event in the ordinary trace
log, so it is findable without inventing a second artifact:

```json
{"event": "verify_cascade", "origin": "glossary set",
 "terms": ["裴小丫"], "chapters": ["CHAPTER_0001.md"],
 "examined": 1, "flagged": 1, "confirmed": 1, "calls": 1,
 "confirmed_chapters": ["CHAPTER_0001.md"], "errors": []}
```

`origin` is the verb the edit came from (`glossary replace|set|merge|retire`, or
`review fix`); `terms` is the set actually written, which for `merge` is both
`--keep` and `--remove`, and for `retire` is the retired source.

Two consequences of matching against the **live** glossary are worth stating,
because they look like a missing feature and are not:

- Retiring or merging a term away leaves nothing for stage 1 to match it
  against, so those verbs normally report `0 flagged by the deterministic
  stage, 0 ... confirmed` while still scoping the scan correctly. The console
  says so explicitly rather than leaving the `0` to be read as a missing
  feature — `[verify] '<term>' is no longer a pinned glossary term, so the
  deterministic stage has nothing to match it against; those chapters were
  re-checked against the glossary as it now stands`. An unpinned term cannot
  contaminate a future chapter — that is the honest answer, and the reason to
  retire is to stop the damage, not to detect it retrospectively.
- A term occurring in no chapter yields an empty scope: the command prints
  `[verify] cascade check after <verb>: '<term>' occurs in no chapter - nothing
  to re-check` and makes no model call.

## Draft artifacts (`draft/`)

For `CHAPTER_0001.md` the pipeline creates:

| File | Contents |
|---|---|
| `CHAPTER_0001.md` | human-readable current translation draft (frontmatter + lines) |
| `CHAPTER_0001.lines.json` | `{"title": "...", "lines": [...]}` — written per attempt as a debug artifact; nothing reads it back |
| `CHAPTER_0001.state.json` | pipeline state: `{"stage", "attempt", "title", "lines", "chunks": [[...], ...] or null, "feedback": [...], "notes": [...], "rejected": [...], "updated_at", "pipeline"}` — `pipeline` is the state-schema version; `title`/`lines` hold the draft translation (crash-resume past TRANSLATE); `chunks` holds per-part TRANSLATE progress (below) |

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
`[CHAPTER_NNNN] [init] resuming translation at part k/n (m lines already
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
  (`<a epub:type="noteref">` → `<aside epub:type="footnote">`), numbered in
  the sidecar's reading order (ascending `line`), so the markers appear in
  the order the reader meets the text. Chapters
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
guaranteed final build at batch end; a stalled build is killed after 360s —
the kill takes the builder's whole process tree, which reaches the docker
CLI and its children, but the daemon-side epubcheck container is not the
builder's child and may run to completion, docker `--rm` reaps it; a kill
that itself fails, or a builder that survives the kill, is warned about,
never raised — `[warn] epub auto-build: failed to kill builder (taskkill:
<error>) - it may still be running` / `[warn] epub auto-build builder
survived the kill - it may still be running` — and a confirmed kill sweeps
the dead build's pid-scoped `export/*.epub.<pid>.tmp` sibling with
`[warn] removed N stale epub temp file(s) left by the killed build`)
and failures are warnings only (output in `logs/epub-build.log`), so
`export/` always holds a current epub; disable with `auto_build_epub:
false`.

## Prompt templates (`templates/`)

The project's `templates/` holds only the prompts **you** wrote. `init` copies
nothing into it and creates it empty; the skill's `assets/templates/` is the
base layer, resolved at read time (§ Overlay layers). A project file wins, a
missing one falls through, and one that is byte-identical to a revision the
skill shipped is an unmodified leftover and falls through too — so a shipped
prompt improvement reaches an existing project on its next run, while a prompt
you edited is never overwritten. `migrate` still runs the template maintenance
pass — missing templates materialized onto disk, differing ones prompted
(`templates ~ <name>.md differs from the shipped copy - overwrite it? [y/N]`;
Enter/n keeps the project's copy, y overwrites; `--force` answers y to all
prompts, and non-interactive runs keep differing copies with a `[warn]`). That
pass runs on already-current projects too, leaving the version stamp untouched.
Plain `{{placeholder}}` substitution. The pipeline
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
| `glossary_verify.md` | `verify` model tier (stage 2; one term per call, batched by term) | `source_lang target_lang term contexts context_count` |
| `notes_review.md` | `review notes` model tier | `source_lang target_lang entries` |
| `style_profile.md` | `--style auto` init / `profile` (legacy) | `source_lang target_lang sample_text` |
| `consensus.md` | arbitration for a job with no `consensus_<job>.md` in either layer | `task_prompt candidates_section` |
| `consensus_translator.md` | arbitration for a `translator` fan-out | `task_prompt candidates_section` |
| `consensus_glossary.md` | arbitration for a `glossary` fan-out | `task_prompt candidates_section` |
| `consensus_reviewer.md` | arbitration for a `reviewer` fan-out | `task_prompt candidates_section` |
| `consensus_annotator.md` | arbitration for an `annotator` fan-out | `task_prompt candidates_section` |
| `consensus_recap.md` | arbitration for a `recap` fan-out | `task_prompt candidates_section` |
| `consensus_profile.md` | arbitration for a `profile` fan-out | `task_prompt candidates_section` |
| `consensus_verify.md` | arbitration for a `verify` fan-out | `task_prompt candidates_section` |

Each job in `CONSENSUS_JOBS` that fans out (two or more provider
blocks) resolves **its own** `consensus_<job>.md`, because what makes one
candidate better is not the same question for each: a translation is judged on
line-indexed structure and pinned terminology, a glossary entry on being
grounded in the source, a reviewer finding on being grounded in the text it
reviews, and a verify verdict on a sense the pinned rendering cannot carry. A
project-local `consensus_<job>.md` beats the shipped one under the
same overlay as every other prompt.

The general `consensus.md` is reached only when `consensus_<job>.md` resolves
in neither layer — when the project and the skill both have no file of that
name, which is the `PipelineError` path. The skill ships
`consensus_<job>.md` for every job in `CONSENSUS_JOBS`, so the per-job name
always resolves and `consensus.md` is not read: a project's
customized general `consensus.md` is bypassed for every fan-out job. A job with a
**single** provider block returns before any template is loaded, so it loads
none.

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

**Whole-chapter translation**: only the OUTPUT is constrained. Two distinct
caps, since v012:

- **the ceiling** — `translate_max_output_tokens` (default 65536 since v009, up
  from the old 8192 — reasoning-capable hosted models share this budget with the
  answer). This is the value each translate call ASKS for;
- **the block cap** — each translator block's **effective** cap,
  `min(max_tokens, max_tokens_limit)`, a hard provider limit the pipeline may
  lower but never raise.

The sent value is `min(ceiling, that block's effective cap)`, computed per block
— so a 256000 ceiling over `[128000, 256000]` sends 128000 to the first model
and 256000 to the second. Chapters whose EXPECTED output fits the packing budget
below are
translated in ONE call — the model sees the chapter's full context (input is
never limited by this). Longer chapters split by greedy per-line token-budget
packing: each source line costs its CJK chars + other chars/4 + 10 (the
numbered-JSON wrapper), parts close when the next line would exceed
floor(0.8 × pack_cap) − 256, where **pack_cap = min(ceiling, the smallest
translator block's effective cap)** (the 0.8 headroom absorbs estimate
error; the 256 is the per-part JSON overhead; every part takes ≥ 1 line), with
style background and the previous part's final lines included as input context.
Packing is deterministic given (source, config), so part bounds — and with
them the `[Rejected Previous Attempt]` feedback slices — reproduce exactly
across attempts and resumes. A part whose response looks truncated (missing
line indices, or a response cut mid-JSON) retries once at
`max(ceiling, min(round(1.5 × pack_cap), the smallest block max_tokens))` — the
guard is keyed on the ceiling, the value the first attempt actually sent, so
the retry can never go below it; when the smallest block is at or below the
ceiling there is nothing above to escalate into and the retry re-sends at the
same cap, which is correct because the part already fits what every block can
return. Other shape
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
  every proposal whose source does not match an existing entry (by source
  or variant) passes the client-side significance gate FIRST: its source
  (plus its proposed variants) is counted across the whole source corpus
  (all `source/CHAPTER_*.md` bodies joined) and it is dropped below
  `min_term_occurrences` occurrences (default 3; 0 disables the gate, and
  an unreadable corpus fails it open) with `[glossary] skip '<src>' - <N>
  occurrence(s) across the novel (min <M>)`. Only gate-passing proposals
  reach the nickname rule: a proposal whose source is contained in a known
  term's source, or contains it, with the same translation
  (nicknames/short forms) is absorbed as a variant of the known entry,
  never a separate entry — so an absorbed nickname is novel-wide
  significant too. A re-proposal whose source matches an existing entry is
  never gated on that source, and real merges of a matched entry stay
  ungated — but any NEW variant the re-proposal carries is counted
  individually across the same corpus and gated like a brand-new term,
  dropped below the floor with `[glossary] skip variant '<v>' - <N>
  occurrence(s) across the novel (min <M>)`; already-present variants
  being restated are never re-gated. The model output schema itself is
  unchanged.
  Category handling end to end: the model is offered the glossary
  categories minus `unit` in `glossary_expand.md`'s enum (units come
  from catalogues, not proposals); a proposal carrying any KNOWN
  category — `unit` included — passes validation unchanged, while a
  proposal with an UNKNOWN category is coerced to `other` with the
  console line `[glossary] warn unknown category '<cat>' for
  '<source>' - coerced to 'other'` (in-pipeline lines carry the
  `[CHAPTER_NNNN]` tag prefix like the other stage lines). That
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
| 1 | `translate` / `retry` — at least one chapter ended `needs-review`; `build-epub` — epubcheck reported errors (the epub failed validation); `review glossary` — warns remain after the run; `review fix` — at least one command failed, or the report was refused as stale; `glossary search` — nothing found; `glossary count` — below the significance threshold; `tn` — failed chapters (annotator call or unreadable chapter) or no eligible chapters in range; `profile` — generation failed; `verify` — at least one chapter had glossary drift CONFIRMED by the model stage, or at least one chapter could not be checked |
| 2 | usage or setup error — bad arguments, missing files, corrupt project JSON (one `[FAIL]` line, never a traceback); a config that resolves no `base_url` (§ Overlay layers); `build-epub` — the builder subprocess crashed; `ping` — one or more providers unreachable; `sync-config` — the skill's `config.local.json` is unreadable/malformed, or the project has no `config.json` |
| 3 | a provider call failed and the run stopped — see "Provider failure is fatal" below |
| 130 | interrupted at the keyboard (`Ctrl-C`) — chapter state is saved, re-run to resume |

`build-epub` splits its failures: epubcheck failing validation exits 1,
the builder itself crashing exits 2. `ping` exits 2 whenever one or more
providers are unreachable.

### Provider failure is fatal

Every provider call that fails **stops the run** (one `[FAIL]` line on stderr,
exit 3, never a traceback). There is no survivor fallback and no partial
chapter. Two kinds:

- **Irrecoverable, per the provider** — raised on the *first* response, with
  no retry and no backoff. Z.AI's table
  (<https://docs.z.ai/api-reference/api-code>, transcribed into
  `client.ZAI_FATAL_CODES`, matched by the body's `error.code` and never by
  message text) covers auth failures (`1000`, `1001`, `1003`, `1005`,
  `1220`), an exhausted balance (`1113`), a request the provider will always
  refuse (`1210`–`1215`, `1221`, `1222`, `1261`), filtered content (`1301`),
  and plan/key problems (`1309`, `1311`, `1313`, `1314`, `1315`). `ping` names
  the code too, so a wrong URL can be told apart from an empty balance.
- **Exhausted** — anything else, after `_MAX_ATTEMPTS` (4) attempts with
  `(2, 4, 8)`s backoff, including every `429` and `5xx`. `429` rate limits
  (`1302`, `1305`) and the quota *windows* (`1308`, `1310`, `1316`–`1321`,
  which publish a reset time a multi-hour run may well clear) stay on the retry
  ladder deliberately: the difference is how many attempts are spent, not
  whether the run survives.

The check runs before the guided-JSON fallback, so a `1214` whose message names
`response_format` cannot drop `response_format` and re-POST the same request.

`client.LLMFatal` inherits from `pipeline.PipelineError`, so a provider failure
passes through every stage guard's `except PipelineError: raise` untouched
instead of being absorbed into retry feedback or a `needs-review` chapter.
Non-provider shape errors are unaffected: a missing template or an unfilled
placeholder is still `PipelineError` and still exit 2.

Re-running after exit 3 is safe. Completed chapters stay `translated`; the
chapter that failed returns to `needs-review` on the next run.
