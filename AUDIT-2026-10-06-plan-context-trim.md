# Adversarial audit — PLAN-2026-10-06-context-trim.md

**Date:** 2026-10-06
**Audited:** `PLAN-2026-10-06-context-trim.md` (context-cost reduction for
`novel-translator`; Phase 1 = move 4 SKILL.md blocks to a new
`references/maintenance.md`)
**Method:** two parallel adversarial agents (forensic fact-check; hostile
design review) under an assume-nothing-works contract, followed by
orchestrator re-verification of every load-bearing finding at source level.
**Working tree:** HEAD `0604f92`, docs-only audit (no code touched).

## Verdict

**Revise before implementation.** The plan's skeleton is verified sound —
section boundaries, the §3.2 cross-reference table, the blast-radius claims,
and the test baseline all reproduced exactly. The defects are concentrated in
the plan's own safety mechanisms: the AGENTS.md mitigation is not executable
as written and would leave a false mirror contract in place (F1), the
`seed` re-run instruction leaves the common batch path with no pointer (F2),
and the trigger lines demonstrably miss realistic phrasings (F3). The size
tables are character counts mislabeled as bytes (F9).

## Findings

### F1 — BLOCKER: the move falsifies AGENTS.md's mirror contract, and the plan's mitigation is not executable as written

`AGENTS.md:17-19` is one prose bullet, not a "list" to append to:

> `novel-translator/SKILL.md` and `novel-translator/README.md` restate
> subcommand semantics, defaults, console markers (`[ok]`/`[warn]`/`[FAIL]`),
> and migrate prompt wording.

All four pinned categories sit inside the moved ranges, so post-move the
sentence is false for SKILL.md:

- **migrate prompt wording** — `SKILL.md:499-500`
  (`templates ~ <name>.md differs from the shipped copy - overwrite it? [y/N]`);
  survives only in `README.md:79-82` and `references/file-formats.md`.
- **console markers** — `SKILL.md:562`, `:568`, `:594-595`, `:696`,
  `:743-751`, `:769`, `:839-841`.
- **defaults** — `SKILL.md:536-537` (`review_batch_size`, default 40),
  `:867-868`, `:878-879`.
- **subcommand semantics / exit codes** — `SKILL.md:518-520`, `:556-562`,
  `:704-705`, `:770-772`, `:879-881`.

Executing §3.2's row as written ("add `references/maintenance.md` to the
mirror list") either appends to the sentence — leaving the false SKILL.md
claim — or has no target at all. AGENTS.md's operative propagation rule
("Defaults, key names, exit codes, flags, console output … must be
propagated to the matching docs") then misroutes every future change to
`review.py` / `glossary.py` / migrations to a SKILL.md section that no longer
holds the content. **The plan must ship the exact replacement wording for the
AGENTS.md bullets, splitting the SKILL.md claim by subcommand.** Adopting F15
(keep migrate in SKILL.md) shrinks this reword to review/glossary/tn.

### F2 — HIGH: the `seed` re-run instruction is common-path operational guidance, not maintenance, and loses its only always-loaded home

`SKILL.md:641-644` (inside the moved Glossary-upkeep block):

> Re-run seeding with `uv run "$SCRIPT" seed --project .` after adding
> chapters or editing a catalogue; `--min-count N` overrides …, `--catalogue
> PATH` (repeatable) adds an explicit catalogue file …

The kept text actively teaches the long-novel batch loop (`SKILL.md:48-50`:
"after each new batch run `sync` … and `translate --next N`"), and
`references/ingestion.md` mentions `seed` zero times (grep-verified). `sync`
does not seed (it rescans, backfills frontmatter, rebuilds the manifest,
commits). Post-move, an agent in the standard ingestion loop has no pointer
to re-seeding, and the §3.1 trigger vocabulary ("audit the glossary, review
or fix terms, apply bulk review fixes, retire entries, rewrite a changed
rendering, re-check translator's notes") contains nothing about seeding or
catalogues — new-batch catalogue terms silently never enter the glossary.
**Fix:** keep the seed sentence in SKILL.md attached to the sync paragraph,
and/or add seeding to the trigger vocabulary.

### F3 — HIGH: trigger-line recall is demonstrably insufficient

Realistic asks that match no §3.1 trigger and no longer have inline docs:

| User phrasing | Command now hidden in maintenance.md |
|---|---|
| "merge the two entries for 青云剑" / "dedupe the glossary" | `glossary merge` (`SKILL.md:843-852`) |
| "chapters 51-60 are in; pick up the new catalogue terms" | `seed` (F2) |
| "stop the glossary from auto-retiring terms" | `glossary_auto_cleanup` (`SKILL.md:635-638`) |
| "why did expansion skip 麦穗?" | `glossary count` diagnostic (`SKILL.md:872-881`; README frames it exactly this way) |
| "add an alt translation for 灵根" | `glossary set --add-alt` (`SKILL.md:835-837`) |
| "what does review-report.md mean" | report walkthrough (`SKILL.md:564-583`) |

The cited precedent is weaker than claimed: `SKILL.md:52-53` defers only
conversion rules/pitfalls/checklist while the full ingestion skeleton stays
inline; the plan's own Migrations stub keeps the command inline too.
**Fix:** spend ~400 B inlining the command surface one-liner (`review
glossary|notes|fix`, `tn`, `glossary set|merge|retire|replace|search|count`,
`seed`) into the glossary trigger bullet and widen the trigger vocabulary.
Do not restate failure-mode prose inline — the command surface is what
prevents stub-reasoning.

### F4 — MEDIUM: maintenance.md becomes the sole carrier of facts file-formats.md does not have, contradicting the Risk-2 preamble plan

`references/file-formats.md` § Exit codes (`:1296-1308`) has **no `migrate`
row and no `glossary set/merge/retire` rows**. Migrate's exit-2 conditions
exist only at `SKILL.md:518-520`; the per-subject review flag table only at
`:557-562`; the category vocabulary list only at `:791-792` (f-f carries
only the error-string instance). Yet the moved text also *duplicates* large
f-f content (`:447-520` ≈ f-f § Migrations; report contract ≈ f-f review-report
sections). An agent believing the planned preamble ("schemas are in
file-formats.md") and looking there for migrate's exit semantics finds
nothing — and kept `SKILL.md:664` promises f-f "enumerates them per
command", which for `migrate` it does not. **Fix:** port the missing
exit-code rows into f-f § Exit codes in the same change (which also makes
the `:664` claim true), or scope the preamble's deferral precisely.

### F5 — MEDIUM: a second positional dependency the ordering note misses

Besides the handled `SKILL.md:583` "below", `SKILL.md:865-866` (moved Bulk
review fixes) reads "to find entries before the **editing subcommands
above**" — resolvable only while `glossary set/merge/retire/replace`
(`:835-862`) precede `search` in the same file. Together with `:522`,
`:555`, `:567`, `:583`, five name/positional references hold only while all
four blocks share one file **in source order**. **Fix:** state the
constraint as "preserve source order and co-location of all four blocks"
(this also settles §8 Q2 — see below).

### F6 — MEDIUM: §6 step 4 ("pure move") contradicts §3's own required transformations

§3 requires heading promotion (`- **Glossary upkeep**: …` → `## Glossary
upkeep`) and Risk 2 requires a preamble line; §6.4 declares any wording
change in moved regions a review finding — the plan's verification would
flag its own required edits. Also `SKILL.md:645` ("Every mutating action in
**this bullet** commits") goes stale under heading promotion and is not
listed in §3.2. **Fix:** restate §6.4 as "pure move modulo heading promotion,
the `:645` 'in this bullet' fixup, and the added preamble."

### F7 — MEDIUM: §6 step 3 cannot detect the plan's own blocking defect

No grep pattern, no topic enumeration, SKILL.md-only scope. Risk 1 declares
a trigger-less pointer blocking, but nothing operationalizes the check.
**Fix:** supply the concrete case-insensitive pattern
(`glossary upkeep|bulk review|translator's-note|re-evaluation|maintenance.md`
plus the moved command names) across `SKILL.md`, `README.md`,
`references/*.md`, requiring every moved topic to retain ≥1 trigger mention.
Note: `scripts/translate.py:95,1672` legitimately contain "glossary upkeep"
as CLI help text — not doc pointers; don't chase them.

### F8 — MEDIUM: the auto-cleanup toggle and un-retire remedy move off the common path while their behavior stays on it

Kept BALANCE-stage text keeps deferred-retirement behavior, but the toggle
(`glossary_auto_cleanup`, `SKILL.md:635-638`) and the remedy (remove source
from `retired` list, `:640-641`) move. A mid-translation "terms keep
disappearing from my glossary" is answerable today from always-loaded text;
post-move it needs maintenance.md and no trigger fires. Covered by the F3
vocabulary fix (add auto-cleanup / retired wording).

### F9 — LOW: every per-range "Bytes" figure in the plan is a character count, not bytes

All values reproduce exactly as decoded char counts (internal newlines
included, final line's newline excluded). On-disk bytes are systematically
larger (CJK + em-dashes):

| Range | Plan | Chars (plan's unit) | On-disk |
|---|---|---|---|
| Operating notes 417-672 | 16,655 | 16,655 | 16,773 |
| Glossary upkeep 521-649 | 8,640 | 8,640 | 8,705 |
| Bulk review fixes 754-881 | 8,226 | 8,226 | 8,267 |
| TN re-eval 673-753 | 5,196 | 5,196 | 5,230 |
| migrate 447-520 | 4,794 | 4,794 | 4,821 |
| **moved total** | **26,856** | **26,856** | **27,023** |

Percentages shift ≤0.6 pp; every conclusion survives. But §6.5's "re-run the
section sizer" will not reproduce the table against `wc -c`, and §4/§6.2
size gates are stated in the wrong unit. Pure-move after-state floor is
28,201 B on disk (~29.4 KB with the ~1 KB of trigger lines — consistent).
Preamble 1-31 = "1,987" reproduces under no convention (1,992 chars /
1,999 B).

### F10 — LOW: Phase 2's figures and premise are wrong

"~24 KB / ~6k tokens" after also moving the stage machine is irreproducible:
29,400 − 11,672 ≈ **17.7 KB** (figure ~35% high). And the "11 docker/epubcheck
failure needles" live in **build-epub** (`SKILL.md:364-371`), not the
stage machine; the stage machine's mechanism claims (token-budget formula,
Levenshtein tolerances) are correctly located. Deferred, so informational —
but must be corrected before any Phase 2 go/no-go.

### F11 — LOW: the dead-code sweep numbers do not reproduce (conclusion holds)

Measured 155 public top-level names for the stated scope (183/205 under
natural extensions; no convention yields 192). "0 with ≤1 repo-wide
reference" is literally false: `JPEG_QUALITY` (cover.py:25→:87), `EpubError`
(epub.py:22→:248), `GIT_USER_NAME`/`GIT_USER_EMAIL` (vcs.py:51-52→:167-168),
`DEFAULT_STYLE_SUMMARY` (styles.py:32→:62) each have exactly one use — all
live. Zero unused imports (confirmed). The substantive conclusion — no
trimmable dead code — holds.

### F12 — NOTE: "three contiguous blocks" is false

The kept bullets at 650-672 sit between Glossary upkeep (ends 649) and TN
re-eval (starts 673); each block is individually contiguous. §3's table is
precise; wording only.

### F13 — NOTE: "code is the other 438.5 KB"

All `.py` = 438.6 KiB, but the complement of docs in the 643.4 KiB payload
is 469.1 KiB — ~30.5 KiB of assets (templates/styles `.md`, catalogues
`.json`) is silently excluded.

### F14 — NOTE: minor citation slips

Token figures (~13.8k before / ~7.4k after) unverifiable (no tokenizer run;
proportional). §2.4 cites "translate.py:50-54" — constants sit at
`scripts/translate.py:50-53`.

## Verified clean (attacked, held)

- **All section and bullet boundaries exact** — no off-by-one at any of the
  19 audited ranges (two agents + orchestrator reads).
- **§3.2 cross-reference table is complete** — full repo sweep (all
  `.md`/`.py`/`.json`/`.toml`) found zero references to moved sections that
  the plan doesn't handle. `README.md:215` resolves to README's own
  `## Bulk review fixes` at `:443`; `file-formats.md:302` "(see SKILL.md)"
  targets the kept needs-review section; `ingestion.md` has zero mentions.
- **Blast radius genuinely nil** — no script (incl. migrations) reads any
  doc; the sole tests match is the docstring at
  `tests/test_main_exit_codes.py:293`; no manifest enumerates doc files;
  the installed skill is a junction to the repo (no sync step).
- **Test baseline exact** — `uv run tests/run_all.py` reports
  "38 passed, 0 failed (38 script(s), 1508 check(s))", matching the plan.
- **Plan Risk 1's error examples are accurate per code** —
  `cmd_review_fix` uses `--glossary` as the report path
  (translate.py:948-949) and rejects `--fix` (:943-946);
  `cmd_review_notes` rejects `--fix` (translate.py:1073-1074).
- The existing Exit-codes pointer survives untouched (`SKILL.md:661-666`).
- All six cut boundaries clean; no TOC/anchor index/line-number references
  exist anywhere; post-move SKILL.md ends sensibly.

## §8 open questions — recommendations

1. **Trigger wording: not sufficient as written.** Inline the command
   surface (~400 B) and widen the vocabulary (F3). Don't inline failure-mode
   prose — knowing `review fix --glossary <report>` exists prevents the
   invent-a-selector failure the plan worries about.
2. **Split maintenance.md further: no — close the question.** Five
   positional/name references (`SKILL.md:522, 555, 567, 583, 866`) hold only
   while the four blocks share one file in source order (F5). A split breaks
   all five, doubles pointer maintenance, and grows the fragile AGENTS.md
   bullet. Revisit only with observed usage.
3. **Relocate the migrate bullet: no — leave it in SKILL.md.** Constraint 3
   itself concedes nobody reads it and "Phase 1 still stands without it"
   (still −22,062 chars ≈ −40%). It is the sole carrier of migrate's exit-2
   conditions (F4), and keeping it leaves "migrate prompt wording" true in
   AGENTS.md:17-19, shrinking F1's reword to review/glossary/tn. If the
   4.8 KB matters later, the §3.1 Migrations stub shows the bullet can be
   compressed in place.

## Required revisions before implementation

1. Ship exact replacement wording for the AGENTS.md mirror bullets (F1);
   simplest if revision 3 is adopted.
2. Keep the `seed` sentence in SKILL.md attached to the sync/loop paragraph;
   add seed/catalogue wording to the trigger (F2).
3. Inline the command-surface one-liner into the glossary trigger bullet;
   widen trigger vocabulary: merge/dedupe, seed, auto-cleanup/retired,
   count/significance, add-alt, report walkthrough (F3, F8).
4. Port migrate + `glossary set/merge/retire` exit rows into
   `references/file-formats.md` § Exit codes, or scope the maintenance.md
   preamble's deferral claim precisely (F4).
5. State the ordering constraint as source-order + co-location of all four
   blocks; close §8 Q2 (F5).
6. Restate §6.4 (pure move modulo heading promotion + `:645` fixup +
   preamble) and give §6.3 a concrete grep pattern and scope (F6, F7).
7. Relabel the size tables as character counts (or recompute on-disk); fix
   the 1,987 preamble figure; correct or flag Phase 2's numbers and premise
   (F9, F10); optionally correct the dead-code counts (F11).
