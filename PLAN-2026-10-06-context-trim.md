# Plan — context-cost reduction for `novel-translator` (v2)

**Date:** 2026-10-06
**Status:** proposal, not yet implemented. Revised after the adversarial
audit (`AUDIT-2026-10-06-plan-context-trim.md`) and owner rulings of
2026-10-06.
**Author scope:** `D:\Repos\vibed\translate-skill` @ HEAD `0604f92`.

**v2 changelog** (all changes trace to audit findings F1-F14 or owner
rulings):

- The migrate bullet **stays in SKILL.md** (audit Q3/F4: it is the sole
  carrier of migrate's exit-2 conditions, and keeping it keeps
  "migrate prompt wording" true for SKILL.md in AGENTS.md).
- AGENTS.md gets **exact replacement wording** for the mirror bullet, not
  "add to the list" (F1; owner ruling: AGENTS.md modification is in scope).
- The `seed` re-run sentence is **carved out and re-homed** next to the
  sync paragraph instead of moving (F2: seeding is common-path).
- The trigger bullet **inlines the command surface** and widens its
  vocabulary (F3/F8: merge, seed, auto-cleanup, significance, add-alt).
- The ordering constraint covers **all five** positional references and
  mandates co-location (F5); §8 Q2 closed — no further split.
- maintenance.md gets a **pinned preamble** that scopes the deferral to
  file formats/config schemas only (F4); file-formats.md stays untouched.
- Verification is **operationalized**: concrete grep pattern, explicit
  pure-move exceptions, on-disk byte gates (F6/F7/F9).
- Size tables relabeled **chars** (the v1 unit) with on-disk bytes where
  they gate anything; v1's 1,987 preamble figure corrected; Phase 2 figures
  and premise corrected (F9/F10); dead-code counts replaced with the
  verified claim (F11).

---

## 1. Goal and binding constraints

The skill has two independent consumers. This is the design, not an accident:

| Document | Consumer |
|---|---|
| `novel-translator/SKILL.md` | an agent |
| `novel-translator/README.md` | a human running the CLI by hand |
| `references/file-formats.md` | schemas — normative mirror of the code |
| `references/ingestion.md` | the agent-driven project-initiation playbook |
| `references/maintenance.md` (new) | an agent doing glossary/review/tn upkeep |

Owner-stated constraints that shape this plan:

1. **Project initiation is agent-driven by design.** The absence of a CLI
   scraping command is **not** a gap; do not propose one; do not "fix"
   `references/ingestion.md`'s agent voice.
2. **Cross-document restatement is not waste** (owner re-confirmed
   2026-10-06: README is human-facing, SKILL.md is agent-facing, duplicates
   are fine). Do not deduplicate between them. README.md is therefore
   untouched even where it restates moved content.
3. **The migrate bullet stays in SKILL.md** (v2 decision). Migration prose
   needs no trimming and nobody reads it during normal operation, but it is
   the only doc home of migrate's exit-2 conditions (SKILL.md:518-520) and
   of the migrate prompt wording that AGENTS.md pins to SKILL.md. If its
   4.8 KB ever matters, compress it in place — do not relocate it.
4. The only goal is **context saving**. Repo byte count is explicitly *not*
   a goal.
5. **AGENTS.md modification is in scope** (owner ruling): the mirror
   contract must be rewritten to match the new doc layout (§3.3).

## 2. Evidence

Measurement convention (v2): per-range sizes are **chars** — decoded
characters, internal newlines included, final line's newline excluded (the
unit v1 used unlabeled). On-disk bytes run ~0.6-1.4% higher (CJK/em-dashes).
Totals that gate anything are given on-disk.

### 2.1 Where the context goes

`SKILL.md` is 55,224 B on disk / 881 lines / 7,655 words / ~13.8k tokens,
and it loads in full on every skill invocation. Section breakdown (chars;
% shifts ≤0.6pp under either unit):

| Section | Lines | Chars | % |
|---|---|---|---|
| Operating notes | 417-672 | 16,655 | 30.2 |
| `translate` stage machine | 134-318 | 11,672 | 21.1 |
| Bulk review fixes | 754-881 | 8,226 | 14.9 |
| Translator's-note re-evaluation | 673-753 | 5,196 | 9.4 |
| `init` | 55-133 | 4,530 | 8.2 |
| `build-epub` | 346-416 | 4,154 | 7.5 |
| needs-review recovery | 319-345 | 1,321 | 2.4 |
| preamble + prerequisites | 1-31 | 1,992 | 3.6 |
| acquire/prepare | 34-54 | 1,089 | 2.0 |

`references/file-formats.md` is 77,678 B but is on-demand. Docs total
174.3 KiB of the 643.4 KiB skill payload (27.1%); `.py` is 438.6 KiB, the
remaining ~30.5 KiB is assets (templates/styles `.md`, catalogues `.json`).

### 2.2 The core finding

Breaking down Operating notes by bullet (L417-672):

| Lines | Chars | Bullet | Verdict |
|---|---|---|---|
| 417-418 | 19 | `## Operating notes` header | keep |
| 419-421 | 179 | tool is fully manual-runnable | keep |
| 422-428 | 425 | Hy-MT2 prompt conventions | keep |
| 429-441 | 874 | providers are per job | keep |
| 442-446 | 298 | templates are per project | keep |
| 447-520 | 4,794 | Upgrading projects (`migrate`) | **keep (v2)** |
| **521-649** | **8,640** | **Glossary upkeep** | **relocate (minus seed carve-out)** |
| 650-652 | 217 | new source language | keep |
| 653-660 | 482 | cost/cadence | keep |
| 661-672 | 718 | output markers + exit-code pointer | keep |

**22,062 chars (~40% of the whole file) sit in three blocks — Glossary
upkeep, Translator's-note re-evaluation, Bulk review fixes — that an agent
does not need in context to translate a chapter.** They are advisory
maintenance workflows. The blocks are each internally contiguous but
mutually separated by the kept bullets at 650-672. An agent running
`translate --next 10` pays for all of it and reads none of it.

One sentence inside the Glossary-upkeep block is **common-path and stays**:
the `seed` re-run instruction (SKILL.md:641-645, starts mid-line). The
long-novel batch loop the kept text teaches (`sync` + `translate --next`,
SKILL.md:48-50 and 99-104) depends on it; `sync` does not seed, and
`references/ingestion.md` never mentions seeding — moving it would leave
new-batch catalogue terms silently unseeded.

### 2.3 What is NOT proposed, and why

- **Code is untouchable.** An AST sweep of `lib/` + `migrations/` found no
  dead code: no name with zero repo-wide references and no unused imports
  (five constants/exceptions have exactly one use each — all live; details
  in AUDIT-2026-10-06 §F11). Trimming code removes functionality.
- **README.md (39,498 B) is untouched** — human product, per constraint 2.
- **`references/file-formats.md` is untouched.** Its exit-code table lacks
  rows for `glossary set/merge/retire` and `review notes` — a pre-existing
  gap this plan does not widen: the pinned maintenance.md preamble (§3.4)
  routes command-level detail to maintenance.md, so no one is deflected to
  file-formats.md for it.
- **The frontmatter `description` (939 B) is untouched** — pure
  trigger-matching; trimming it costs recall to save ~250 tokens.
- **No new migration ships.** Docs-only: no `config.json` schema key and no
  shipped template under `assets/templates/` is touched, so AGENTS.md's
  migration rule does not trigger.

### 2.4 Blast radius is nil (verified, not assumed)

- No code reads the docs: `scripts/translate.py:50-53` defines only
  `SKILL_ROOT`, `ASSETS_DIR`, `CATALOGUES_DIR`, `TEMPLATES_SRC_DIR`; nothing
  in `scripts/` references `references/` or `SKILL.md` (the phrases
  "glossary upkeep" at translate.py:95/1672 are CLI help text, not doc
  pointers).
- No test reads them: the only match across `tests/**` is a docstring at
  `test_main_exit_codes.py:293`, not an assertion. No manifest enumerates
  doc files.
- The installed skill is a junction to this repo — edits propagate, no
  sync step.
- Baseline reproduced by the audit: `uv run tests/run_all.py` →
  **38 passed, 0 failed (1508 checks)**.

## 3. The change set

Create **`novel-translator/references/maintenance.md`** by moving these
three blocks out of SKILL.md, in file order:

| Source | Lines | Chars | Becomes |
|---|---|---|---|
| `## Operating notes` -> Glossary upkeep bullet | 521-649 | 8,640 | `## Glossary upkeep` |
| `## Translator's-note re-evaluation` | 673-753 | 5,196 | `## Translator's-note re-evaluation` |
| `## Bulk review fixes` | 754-881 | 8,226 | `## Bulk review fixes` |

**Carve-out:** the seed sentence (below) is removed from the moved text and
re-homed in SKILL.md; the commit-summary sentence at 645-649 moves but is
reworded (loses its seed clause and "in this bullet"). Both pinned in §3.1.

**Ordering constraint (updated per audit F5):** the three blocks must be
co-located in one file **in source order**. Five references hold only under
that invariant: SKILL.md:522 and :555 ("see Bulk review fixes"), :567 ("see
Translator's-note re-evaluation"), :583 ("…in Bulk review fixes **below**"),
and :866 ("the editing subcommands **above**" — `glossary search` referring
back to `set/merge/retire/replace` at :835-862). Reordering or splitting is
therefore out of scope (closes v1 §8 Q2).

### 3.1 Pinned new/changed text in SKILL.md

The v1 precedent claim (SKILL.md:52-53) was weaker than stated — ingestion
keeps its full skeleton inline — so the trigger bullet carries the command
surface itself (~0.8 KB against ~22 KB saved), per audit F3.

**(a) Trigger bullet** — replaces the Glossary-upkeep bullet in place
(between the migrate bullet above and the "- **New source language**"
bullet below):

```markdown
- **Glossary and notes maintenance**: hand-fixes to `glossary.json` are
  safe any time — the balance check reads it fresh for every chapter. The
  maintenance workflows — `review glossary` (entry audit, `--fix`),
  `review notes` + `tn` (translator's-note quality and re-check),
  `review fix` (apply a review report), `glossary
  set|merge|retire|replace|search|count`, `util replace` — are documented
  in `references/maintenance.md`; read it before running any of them, or
  when the user asks to audit, fix, merge/dedupe, or retire glossary
  terms, propagate a changed translation, re-check notes, walk through
  `review-report.md` findings, change auto-retirement
  (`glossary_auto_cleanup`, the `retired` list), or check a term's
  significance (`glossary count`).
```

**(b) Seed sentence re-homed** — appended to the sync paragraph (after
SKILL.md:104 "…run it after every download batch."). Text is the existing
sentence, verbatim, plus its commit subject (which moves out of the
commit-summary sentence):

```markdown
Re-run seeding with `uv run "$SCRIPT" seed --project .` after adding
chapters or editing a catalogue; `--min-count N` overrides the seed
threshold for the run, and `--catalogue PATH` (repeatable) adds an
explicit catalogue file, bypassing the language filter (commit subject:
`seed: N glossary term(s)`).
```

**(c) :331 rewrite** — "see Glossary upkeep" becomes
"see `references/maintenance.md`" (full clause: "…`glossary replace` beats
retranslating — see `references/maintenance.md`)").

**(d) Commit-summary fixup inside the moved text** — SKILL.md:645-649
becomes, in maintenance.md:

```markdown
Every mutating action in this section commits — `review: glossary audit`
(suffixed `review: glossary audit (N fix(es) applied)` when `--fix`
landed fixes), `glossary replace` / `util replace: '<src>' -> '<dst>'` —
while read-only `glossary search` / `glossary count` commit nothing.
```

(drops "in this bullet" — stale under heading promotion — and the seed
clause, now carried at the seed sentence's new home).

The migrate bullet (447-520) and the exit-codes pointer (:661-672,
"`references/file-formats.md` § Exit codes enumerates them per command")
are untouched.

### 3.2 Cross-reference corrections

| File:line | Current text | Action |
|---|---|---|
| `SKILL.md:331` | "see Glossary upkeep" | rewrite per §3.1(c) |
| `SKILL.md:522` | "see Bulk review fixes" | inside moved text — internal, no change |
| `SKILL.md:555` | "see Bulk review fixes" | inside moved text — internal, no change |
| `SKILL.md:567` | "see Translator's-note re-evaluation" | inside moved text — internal, no change |
| `SKILL.md:583` | "specified in Bulk review fixes below" | inside moved text — ordering constraint (§3) |
| `SKILL.md:866` | "the editing subcommands above" | inside moved text — ordering constraint (§3) |
| `SKILL.md:641-649` | seed sentence + commit summary | carve-out + rewording per §3.1(b)/(d) |
| `README.md:215` | "see Bulk review fixes" | **no change** — resolves to README's own section at :443 |
| `references/file-formats.md:302` | "(see SKILL.md)" | **no change** — targets the kept needs-review section |

Audit-verified complete: a full repo sweep found no other references to the
moved sections.

### 3.3 AGENTS.md replacement wording (F1)

Replace the bullet at AGENTS.md:17-19 with these two bullets, verbatim:

```markdown
- `novel-translator/SKILL.md` and `novel-translator/README.md` restate
  subcommand semantics, defaults, console markers (`[ok]`/`[warn]`/`[FAIL]`),
  and migrate prompt wording for the pipeline and project commands.
  README, written for humans running the CLI, restates the maintenance
  workflows as well.
- `novel-translator/references/maintenance.md` restates subcommand
  semantics, defaults, and console markers for the maintenance commands
  (`review glossary|notes|fix`, `tn`, `glossary
  set|merge|retire|replace|search|count`, `util replace`); SKILL.md
  carries only their trigger line.
```

### 3.4 maintenance.md preamble (F2/F4 mitigations)

The file opens with, verbatim:

```markdown
Agent-facing workflows for glossary, review-report, and translator's-note
maintenance: semantics, flags, defaults, console markers, and exit
behavior for `review glossary|notes|fix`, `tn`, `glossary
set|merge|retire|replace|search|count`, and `util replace`. File formats
and config schemas live in `references/file-formats.md`.
```

This scopes the deferral to formats/schemas only — command-level detail
(including the per-subject review flag table and the glossary command exits
that file-formats.md does not carry) stays here.

## 4. Expected result

On-disk arithmetic: remove 22,202 B (three blocks) − ~265 B (seed sentence
stays) = 21,937 B; add ~855 B (trigger bullet ~790, seed commit note ~50,
:331 rewrite ~15).

| | Before | After |
|---|---|---|
| SKILL.md | 55,224 B | ~34,100 B (**-38%**) |
| ~tokens on skill invoke | ~13,800 | ~8,500 |
| references/maintenance.md | — | ~22.2 KB (on-demand) |

Total repo size is essentially unchanged. That is intended — the win is
what the agent loads, not what ships.

## 5. Risks

1. **Silent capability loss.** An agent asked to "merge the two entries"
   that does not read the reference will reason from a stub. Mitigation:
   the §3.1(a) trigger bullet names the trigger conditions *and* the full
   command surface — an agent that knows `review fix --glossary <report>`
   exists will not invent a selector. A trigger-less or command-less bullet
   is a blocking defect.
2. **Wrong-reference resolution.** maintenance.md sits next to
   file-formats.md, which also documents glossary/review *formats*. The
   §3.4 preamble routes command detail here, formats there.
3. **AGENTS.md rot.** Solved structurally by §3.3's exact wording;
   residual: future maintenance-command additions must update the command
   list in the second bullet. The §6.3 grep catches drift.
4. **Non-goal risk.** A future change may over-apply constraint 2 and merge
   README into SKILL.md or vice versa. That destroys a consumer; reject.
5. **Carve-out boundary.** The seed sentence starts mid-line (641) and the
   commit sentence continues mid-line (645); the split is sentence-level,
   not line-level. Implement against the pinned texts in §3.1(b)/(d).

## 6. Verification

1. `uv run tests/run_all.py` → must report **38 passed, 0 failed (1508
   checks)** (docs-only, so a no-op confirmation — but AGENTS.md requires
   it).
2. `git diff --stat` shows exactly: modified `novel-translator/SKILL.md`,
   `AGENTS.md`; new `novel-translator/references/maintenance.md`. No
   changes under `scripts/`, `assets/`, `tests/`,
   `references/file-formats.md`, `references/ingestion.md`, `README.md`.
3. Dangling/missing-pointer grep (concrete pattern, per audit F7):

   ```bash
   grep -niE "glossary upkeep|bulk review|translator.s-note|re-evaluation|maintenance\.md" \
     novel-translator/SKILL.md novel-translator/README.md \
     novel-translator/references/*.md AGENTS.md
   ```

   Pass criteria: in SKILL.md, each of the four workflow groups — entry
   audit (`review glossary`), notes (`review notes`/`tn`), report
   application (`review fix`), entry editing (`glossary
   set|merge|retire|replace|search|count`, `util replace`) — appears in the
   trigger bullet, and :331 points at `references/maintenance.md`; README
   hits resolve to README's own sections; `grep -n "glossary upkeep"
   novel-translator/scripts/translate.py` still returns exactly the two
   CLI-help hits (:95, :1672), which are not doc pointers.
4. Pure-move check, restated per audit F6: `git diff` on the moved regions
   must show no wording changes **except** (a) heading promotion of the
   Glossary-upkeep bullet opener (with de-indent of its continuation
   lines), (b) the §3.1(d) commit-sentence rewording, (c) the seed
   carve-out, (d) the §3.4 preamble. Anything else in moved text is a
   review finding.
5. Re-run the section sizer on the new SKILL.md in **on-disk bytes**
   (`wc -c`): expected ~34,100 B ± 300. The three moved blocks must sum to
   ~21,937 B removed.

## 7. Phase 2 — explicitly deferred, do not bundle

Splitting the `translate` stage machine (L134-318, 11,672 chars) into a
`references/pipeline.md` would take SKILL.md from ~34.1 KB to **~23 KB /
~5.8k tokens**. Corrected premise (audit F10): the mechanism in that block
is the token-budget formula (L156-157) and the Levenshtein tolerances
(L185-189); the docker/epubcheck failure needles live in `build-epub`
(L346-416), which Phase 2 does not touch, and much of the stage machine is
agent decisions, not mechanism.

**Recommended against bundling.** The block is read *during* active use;
a missed pointer hits the common path. Only proceed after Phase 1 is in use
and its real-world benefit is observed.

## 8. Decisions (v1 open questions, all closed)

| # | Question | Decision | Basis |
|---|---|---|---|
| D1 | AGENTS.md handling | Modify, with the §3.3 exact wording | Owner ruling 2026-10-06; audit F1 |
| D2 | README/SKILL duplication | Keep; README untouched | Owner ruling 2026-10-06 |
| D3 | Trigger-line sufficiency (v1 Q1) | Inline the command surface; no failure-mode prose | Audit F3 |
| D4 | Split maintenance.md further (v1 Q2) | No; co-location + source order are load-bearing | Audit F5 (five positional refs) |
| D5 | Relocate migrate bullet (v1 Q3) | No; it stays in SKILL.md | Audit F4; constraint 3 |
