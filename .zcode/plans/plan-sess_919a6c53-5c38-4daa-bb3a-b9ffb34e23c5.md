# Preset style guides instead of model-discovered style

Replace the init-time style-profile LLM call with a library of preset style guides chosen at scaffold time. Presets become the default (init makes **zero LLM calls** by default); the existing model-generated profile stays as `--style auto`. Downstream plumbing (`{{style}}` bracket frame, `[Background Information]` frame) is untouched — only the *source* of the text changes.

## 1. New: `assets/styles/` — 4 preset guides (the core deliverable)

File format (name = filename stem; body goes verbatim into the translation prompt's `[{{style}}]` frame — multi-line bracket content is fine per Hy-MT2's style-frame usage; bodies must not contain `{{...}}` since `pipeline.fill` raises on those):

```
description: <one line, shown by the `styles` subcommand>
---
<guide body>
```

**Shared prose-economy block** (in all four, your preference baked in):
- Leanest natural word form: *unease* not *uneasiness* or *uneasy feelings*; "she hesitated" not "she felt a moment of hesitation".
- Cut webnovel filler ("couldn't help but", "a trace of", "slightly") when it adds nothing — but **never omit content**: every line stays a line, every beat a beat. Compression is word choice, not omission.
- Strong verbs over adverb+verb pairs.

**classic.md** (the default) — measured, concise literary English; slightly formal cadence for elder/sect dialogue; no fake-archaic pastiche (no thee/thou/'tis) unless the source itself is archaic; combat = concrete verbs, short sentences; honorifics/titles strictly per glossary.

**transmigration.md** — a register map with three voices:
- Protagonist (transmigrator's inner monologue **and** dialogue): modern casual English — contractions, contemporary idiom, internet-meme phrasing.
- World characters (sect elders, cultivators, mortals): the classic register.
- Narration: follows whoever/whatever it describes.
- CN internet memes/slang → nearest living English internet equivalent (keeps the joke, no footnote); if none exists, translate the meaning in light casual phrasing; footnote only when the joke depends on untranslatable specifics (aligns with the existing TN `threshold` field).
- The archaic-vs-modern contrast is deliberate comedy — never smooth one voice into the other.

**modern.md** — contemporary natural English throughout; dialogue-forward pacing; slang → current English slang; clean unadorned narration.

**literary.md** — imagery-forward epic register, longer cadence allowed, but the same word-level economy: richer imagery, never redundancy; preserve the source's restrained metaphors.

## 2. New: `scripts/lib/styles.py`

- `list_styles(project_dir)` → [(name, description)]: skill `assets/styles/*.md` merged with project `styles/*.md` (project wins on name collision), sorted. Project dir gives humans/agents a place to add their own presets.
- `load_style(project_dir, name)` → body string (everything after the `---` line; whole file if no header). Raises `StyleError` listing available names if not found. Resolution: project `styles/` first, then skill assets.
- Assets-dir location reuses the same logic translate.py already uses to find `assets/templates` for its init glob.

## 3. `scripts/translate.py`

- `init --style NAME|PATH|auto` (default `classic`):
  - NAME → copy preset body to `project/style.md`, record `"style": NAME` in novel_info.json. No LLM call.
  - PATH (existing .md file) → copy it, record the stem as the style name. Lets you keep personal guides outside the skill.
  - `auto` → the existing `generate_profile` path unchanged (novel_info.style_profile).
- `init --background "text"` → writes top-level `novel_info.background` (2–4 sentences; the scaffolding agent can also fill this later from the novel's synopsis/source page — SKILL.md will say so).
- `--skip-profile` kept as a deprecated no-op alias (default behavior is now already LLM-free); help text updated.
- New `styles` subcommand: prints the name + description table (project overrides included).
- `profile` subcommand: unchanged (auto regeneration).
- `status`: prints the active style (novel_info.style name / "auto profile" / legacy fallback).
- Init output prints the available style names when the default was used, so manual users discover the list.

## 4. `scripts/lib/pipeline.py` — resolution order only (~lines 546–561)

- `{{style}}`: `project/style.md` content (header stripped if present) → legacy `novel_info.style_profile.style_summary` → `DEFAULT_STYLE_SUMMARY`. Old fixture projects (sotn, fixture, advisory-live) keep working with zero migration.
- Background: `novel_info.background` (new) → legacy `style_profile.background` → empty.
- No template changes; `{{style}}` still receives a plain string.

## 5. Docs

- **SKILL.md**: init section rewritten — presets table, instruction for the agent to *choose deliberately* (transmigration detection: modern-Earth protagonist, system/isekai tropes, meme usage in the source), `--style`/`--background` flags, `styles` subcommand, project `style.md` hand-editability, `auto` fallback.
- **README.md**: init step, `styles` in the cheat sheet, note that `style_sample_*` config keys now only apply to `--style auto`.
- **references/file-formats.md**: novel_info schema gains `style` + `background`; document `style.md` and the project `styles/` override dir; mark `style_profile.md` template as auto-only.

## 6. Tests / fixtures

- `mock_server.py` style branch: keep (auto path still exists).
- Existing fixture projects: untouched (legacy fallback).
- Mock regression: fresh init with `--style transmigration` + `--background` → translate → epubcheck green, proving preset mode end-to-end; one `--style auto` init to prove the fallback path still works.

## 7. Verification & ship

1. Unit checks (`python -c`): list/load, override precedence, header parsing, missing-name error lists available styles.
2. Live spot-check: translate one real sotn chapter with a preset; eyeball the rendered prompt + response in `logs/llm-*.jsonl`.
3. Sync installed copy `~/.zcode/skills/novel-translator`.
4. Commit to main, push to origin.

Implementation per your global workflow: dispatch parallel subagents per file group (styles lib + presets / translate.py / pipeline / docs), then QA with the Claude code-reviewer agent only (no Kimi/OpenCode — they're broken).

Out of scope: catalogues, balance matching, temperature tuning.