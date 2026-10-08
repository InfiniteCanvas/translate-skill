# Adversarial audit, round 2 — `novel-translator`

**Date:** 2026-10-03
**Commit audited:** `0604f92` (HEAD at time of audit; rounds 1–3 of remediation for
`AUDIT-2026-10-03.md` already landed)
**Scope:** `novel-translator/scripts/**`, `novel-translator/assets/**`, `novel-translator/*.md`,
`novel-translator/references/*.md`, `tests/**`
**Baseline:** `uv run tests/run_all.py` → **38 passed, 0 failed (1508 checks)**

## Method

Five adversarial agents, one per workstream, dispatched in parallel under a stricter protocol
than round 1: **assume nothing works**. Code reading counted as hypothesis formation, never as
evidence — every claim that something works required an observed run (command, exit code, output
or artifact), and every claim that something was broken required a live reproduction. Anything
not proven was reported `UNVERIFIED` instead of guessed.

| WS | Workstream | Files |
|---|---|---|
| WS1 | Pipeline + LLM client correctness | `translate.py`, `pipeline.py`, `client.py`, `config.py`, `project.py`, `profile.py`, `logger.py` |
| WS2 | Doc-mirror drift (claims proven by execution) | `SKILL.md`, `README.md`, `references/*.md`, `assets/templates/**` vs their code mirrors |
| WS3 | Glossary / TN / review / fix / replace / story logic | `glossary.py`, `balance.py`, `tn.py`, `tn_recheck.py`, `review.py`, `review_notes.py`, `fix.py`, `replace.py`, `story.py` |
| WS4 | Output + external-process layer | `epub.py`, `assemble.py`, `cover.py`, `autobuild.py`, `vcs.py`, `styles.py` |
| WS5 | Tests + migrations + dead code (mutation testing) | `tests/**`, `migrations/**`, dead-code sweep of `lib/` |

Techniques used, all against live processes in throwaway temp projects (mock LLM server on
per-agent ports, hand-written flaky/slow HTTP proxies, compiled `docker.exe` PATH shims, real
`build-epub` child processes killed and timed, fuzzed hand-edited reports, byte-level EPUB
unzip inspection, scratch-copy mutation testing). The repo itself was never modified: after all
agents finished, `git status` showed only the pre-existing untracked `.zcodeignore`.

Every finding below was re-verified at source level by the orchestrator before publication.

**Result:** 13 findings (5 medium, 7 low, 1 informational). **No critical or high findings.**
Every round-1–3 remediation in scope was attacked and **held** — the 38 fixes from
`AUDIT-2026-10-03.md` demonstrated their fixed behavior under live exercise, and 11 of 12
high-value mutations injected into the code were killed by the exact tests the remediation
rounds claimed as pins.

---

## Medium

### M1 — A translation/target ending in non-word punctuation can never match; `glossary replace` / `util replace` silently rewrite nothing while reporting success *(borderline high)*
`lib/replace.py:43` · code · found by WS3

`build_matcher` anchors its regex with `\b` on both sides. The tail is `re.escape(words[-1])`,
so for an old translation like `Mr.` the pattern becomes `\bmr\.\b` — a `\b` between a
non-word character and end-of-line can never fire. The phrase "Mr." present verbatim in a
chapter body matches zero times.

- **Observed end to end:** chapter body contains `Mr.`; `glossary replace --source 先生
  --translation Mister` (old `Mr.`) prints `[warn] no occurrences of 'Mr.' found (3 chapter(s)
  scanned)`, **commits and exits 0**, and the glossary now says `先生 → Mister` while the
  chapter still reads `Mr.`. The drift is unrecoverable by re-running (the re-run is a noop),
  `util replace --source "Mr." --target Mister` fails identically, and balance then counts the
  new translation 0 times — every surviving `Mr.` raises a false drift signal.
- **Same construction** at `lib/balance.py:106` (phrase branch); the single-word branch
  (`:111–117`) is only accidentally rescued for len ≥ 5 by fuzzy matching, so **replace and
  balance even disagree** ("Today.": balance counts 1, replace matches 0).
- **Fix:** strip trailing non-word punctuation from the matched phrase and re-append it in the
  replacement, or end the pattern with the inflection group + `(?=\W|$)` only when the last
  character is a word character; mirror in `balance.count_in_target`. `glossary_replace`'s
  pre-flight `build_matcher(old)` (`replace.py:211`) could also warn when the old translation
  ends in a non-word character.

### M2 — The test runner ignores the summary's failed-count; a per-script tail regression passes silently with its evidence suppressed *(borderline high)*
`tests/run_all.py:51,75` · code · found by WS5

`_checks()` reads only `matches[-1][0]` — the passed-count. `main()` computes
`code = proc.returncode if checks else 1`, so a script that prints `N passed, M failed` with
M > 0 but exits 0 **passes**; and because output is printed only for failing scripts, the
contradictory summary is invisible too. Each of the 38 test scripts carries its own ~6-line
tail copy, so one bad tail edit anywhere (a `check(..., False)` followed by `return 0`) is
unpunishable.

- **Observed:** a copy of the real `test_logger.py` with one induced `check(..., False)` and
  tail changed to `return 0` printed `25 passed, 1 failed` and the runner reported
  `PASS  test_z7_tail_bug.py`.
- **Fix:** parse both counts and fail when `failed > 0` regardless of exit code
  (`if int(matches[-1][1]) > 0: code = 1`), or at minimum print failing-check counts for
  passing scripts.

### M3 — Concurrent CLI processes on one project: manifest lost-update silently un-translates a finished chapter
`lib/project.py:178-185` (`save_manifest` is a lock-free whole-file write) · code · found by WS1

No project lockfile exists anywhere. Last-writer-wins on the whole manifest: two `translate`
processes on different chapters of one project both exit 0, but the slower one saves a manifest
in which the faster one's chapter is still `pending` — clobbering its `translated` status while
the translated file and its git commit exist. The next `translate --next` re-picks the chapter
and burns a full chapter of API spend; nothing surfaces the loss (atomic writes hold, so the
JSON is never torn — the loss is purely logical).

- **Observed:** two concurrent processes on a 4-chapter temp project via a slow proxy — exit
  codes `{A: 0, B: 0}`; chapter 2's file exists with its commit, but the manifest reads
  `('Chapter_0002.md', 'pending')`.
- **Fix:** advisory lockfile in the project dir (O_EXCL create) or re-read-and-merge the
  manifest immediately before each save. No doc promises concurrent-process safety, so this is
  a hazard rather than a doc violation.

### M4 — Doc drift: the null/non-numeric-config exit-code contract is wrong for `translate` / `retry`
`SKILL.md:669-671`, `references/file-formats.md:239-244` vs `lib/pipeline.py:1571-1582` · docs · found by WS2

Both docs promise that a numeric config key set to a non-numeric value or null prints **one**
`[FAIL] config key '<key>' must be a number (got <value!r>)` line and exits **2**. On the
translate/retry path the error is raised inside `run_chapter` and swallowed by run_range's
per-chapter `except Exception`, which marks the chapter `needs-review`; the run then exits **1**
with one `[FAIL]` per chapter.

- **Observed** (temp project, `"max_attempts": null`, one pending chapter):
  `[FAIL] Chapter_0005.md: config key 'max_attempts' must be a number (got None)` →
  `[FAIL] chapters need review: Chapter_0005.md` → **exit 1**. A batcher keying on exit codes
  reads "content needs review" instead of "config is broken", and every chapter in the range is
  demoted to `needs-review`. The doc claim *is* true where the key is read via
  `config.get_number` outside `run_range` (verified live on `seed`).
- **Fix:** scope the doc sentence ("except under `translate`/`retry`, where the bad chapter is
  marked `needs-review` and the run exits 1"), or hoist numeric validation before the chapter
  loop so it reaches `main()`'s exit-2 mapping.

### M5 — The hybrid-thinking guard is unpinned: a survived mutation
`tests/mock_server.py:135-151`, `lib/client.py:182-185` · tests · found by WS5

WS5's mutation MU8 deleted the mock server's thinking simulation entirely (the failure mode the
per-provider `thinking` toggle exists to defeat). **Every test stayed green** — no test
references `thinking`/`enable_thinking` at all, no test drives real HTTP through
`mock_server.py` (all pipeline tests fake `_chat`), so reverting `client.py:185` (the `thinking`
key's only wire effect, present in all 6 `PROVIDER_DEFAULTS` blocks) would also go unnoticed.

- **Fix:** one in-process client test against a socket-bound mock server asserting
  `enable_thinking: false` is sent when `provider_cfg["thinking"]` is false and omitted
  otherwise — or wire `mock_server.py` into at least one pipeline test.

## Low

### L1 — `- Command:` bullets inside fenced "do NOT run" example blocks are executed
`lib/fix.py:63,155-177` · code · found by WS3

`_parse_explicit` is purely line-based; it has no fence awareness. A report consisting only of
a fenced example block quoting `- Command: glossary retire --source 灵根` applied the
retirement (exit 0). The writer never emits fenced bullets, so this only bites hand-authored
or agent-authored reports — which are exactly the threat model the smuggle guard defends
against. Fix: track ``` fences in `_parse_explicit`, or document that any `- Command:` line
anywhere is live.

### L2 — Legacy synthesis recovers merge commands from model-tier reason prose
`lib/fix.py:221-233` · code · found by WS3

`_HEURISTIC_DUP_RE` is applied to every `- Reason:` line and `merge_with` is set whenever
`kind == "duplicate"`, while the `- Tier:` value is stored ungated — so a finding headed
`- Tier: model` whose reason happens to match the heuristic template synthesizes and runs a
real `glossary merge`, contradicting the module's own "never by parsing model prose" contract
(`fix.py:9-10,182-184`). Bounded (exact template, allowlisted verbs). Fix: gate both regex
recoveries on `origin == "heuristic"`.

### L3 — An overlong `title_translated` crashes the build as "builder crashed" (exit 2)
`lib/epub.py:302,308` · code · found by WS4

`out_path = out_dir / f"{_slugify(title)}.epub"` has no slug length cap. A 400-char title
(a free-text field a user or model can set) dies with
`OSError: [WinError 123] The filename... is incorrect`, surfaced as
`[FAIL] epub build failed: OSError: …` + exit 2 — indistinguishable from a real builder crash.
Fix: cap the slug in `_slugify` (~150 chars, keeping the `novel` fallback) or catch the OSError
with a "shorten title_translated" hint.

### L4 — Stale `export/*.epub.<pid>.tmp` from a builder killed outside the scheduler is never swept
`lib/epub.py:311` vs `lib/autobuild.py:211-222` · code · found by WS4

`write_epub` unlinks only its own tmp; the round-3 sweep in autobuild fires only on the
abort/stall kill of its own child. A builder dying any other way (external taskkill, power
loss, OOM) leaves its tmp forever — verified: a planted decoy survived subsequent successful
builds, a pre-build crash, and a mid-write crash. Docs honestly scope the sweep to the kill
path, so this is cruft accumulation (gitignored), not a doc mismatch. Fix: sweep foreign-pid
tmps older than some age at build start, or document.

### L5 — `status` on a nonexistent/uninitialized directory exits 0 with an empty table
`translate.py:718-753` via `lib/project.py:170-175` · code · found by WS1

`load_manifest` returns `[]` for a *missing* manifest, so `--project <nonexistent> status`
prints the header, zero counts, `glossary: 0 terms` — exit 0. Every other command treats a
missing project as a setup error (exit 2), and a script grepping status output cannot tell an
uninitialized project from "all chapters pending". Fix: raise `CliError` when the manifest file
is absent.

### L6 — Cosmetic test lint
`tests/test_autobuild.py:551,578`, `tests/test_review_notes.py:684,696`,
`tests/test_glossary_counting.py:181,183`, `tests/test_sync.py:247`,
`tests/test_fix_guard.py:1030` · tests · found by WS5

Duplicate check names across Windows/POSIX and try/except arms (failure listings can't
disambiguate which arm broke) and one captured-but-never-asserted `out2`. No counting impact
(totals reconcile to 1508). Fix: unique names; assert or drop `out2`.

### L7 — Windows NUL stdin masquerades as a TTY in `cmd_migrate`
`translate.py:597` · code · found by WS5

`sys.stdin.isatty()` is true for the NUL character device, so `migrate < /dev/null` on Windows
took the interactive-confirm branch instead of the documented "never prompt" non-interactive
path (no hang — EOF is caught → False). The doc contract holds for genuine pipes. Optional fix:
treat EOF-during-first-prompt as non-interactive, or scope the doc claim to "piped".

## Informational

- **`ping`'s chat-probe fallback reports `[ok]` for a server whose chat always returns empty
  content** (`translate.py:486-500`, `client.py:100-127`): `probe()` sends a minimal body
  without `chat_template_kwargs`, so against a hybrid-thinking-only server ping says
  `[ok] ... (chat ok)` while every real call fails. This is `probe`'s documented tradeoff
  ("any 200 with choices proves routing + auth"), observed live. No change requested; noted in
  case probe should respect `thinking`.

## Round-1–3 remediations: re-verified by exercise, all held

Every fix claimed by `AUDIT-2026-10-03.md` and its three remediation rounds that falls in this
audit's scope was attacked again, with live evidence:

- **H1 smuggle class:** `--proj` / `--pro=` / `--project=` variants against a hand-edited
  report → children reject (`unrecognized arguments`); exact `--project` bullets → guarded;
  an `rm -rf` bullet → parse-time unsupported-verb refusal; the targeted project's glossary
  sha256 unchanged before and after. All 26 parser layers set `allow_abbrev=False`.
- **Dry-run/real-run guard agreement:** skip sets and `needs_decision` counts identical across
  smuggle, conflict, and category reports; dry-run wrote nothing.
- **H5 conflict keys:** `replace --translation A` then `set --translation B --definition D` —
  second command skipped whole, definition untouched; replace shares `set:translation`; per-field
  claiming demonstrated on the CLI.
- **H4 variant gate incl. the round-2 union_variants residual:** zero-occurrence new variant
  dropped with the documented line; restated variants, matched-entry sources, and real merges
  never gated; nickname absorption gated first.
- **M8 category vocabulary:** off-vocabulary `--category` statically skipped with readable
  *and renamed* (unreadable) glossary.json, including under `--exit-on-error` (remaining
  findings still applied); dry-run agrees.
- **M12 replace retry:** 14-second lock hold → exhaustion at 6.71 s wall, exit 2, exact
  `[warn] replace incomplete: 0/3 chapters rewritten...` line, glossary sha unchanged, no stray
  tmp; clean re-run idempotent (3/3).
- **M7 unit warnings:** identical shared warning text from all four assignment paths
  (pipeline new-term, pipeline merge, `glossary set` both flag orders, `apply_fixes`).
- **M10 docker needles + M9 tri-state:** compiled `docker.exe` shim through the real CLI —
  daemon-socket exit 1, exit 125, and `pull access denied` all classify as infra
  (`[warn] epubcheck skipped`, exit 0); unknown-text exit 1 honestly reads as validation
  failure; all 11 needles present at `epub.py:337-352`; dockerless `util replace` →
  `[warn] epub auto-build could not run (epubcheck unavailable)` exit 0.
- **Ctrl-C hygiene:** real hung `build-epub` child aborted — pid-scoped tmp swept with a
  planted foreign-pid decoy kept, survivor/taskkill-failure warns fire, `_kill_tree` never
  raised; finalize-stall warn format matches the doc at real constants.
- **`init --force` subjects:** all four commit subjects distinct, matching file-formats.md
  verbatim.
- **M2 TN zero-over-baseline, stale-report refusal (exit 1 + `--stale-ok`), story-recap prune
  honesty, status-degrade on corrupt inputs, H3 state reset only with `--force`, M5/M6 config
  coercion + escalation caps, BOM/CRLF tolerance, thinking-toggle defeat of the mock trap
  (15/15 requests carried `enable_thinking:false`), chunk crash-resume with no retranslation.**

## Migrations, runner, suite integrity (WS5)

- **Migration chain proven end to end:** pre-v001 project migrated through v001→v007 in order
  with exact DESCRIPTION lines; add-only fold preserves user keys; per-step stamping + one
  commit per step; idempotent second run; `--dry-run` writes nothing (tree sha unchanged);
  non-interactive keeps drift with the documented warn; `--force` refreshes and commits; repo
  backfill on a current project; a fake `v008.py` is picked up by `chain()`/`init`/`migrate`,
  and a mismatched `VERSION` is rejected loudly (exit 2, no traceback).
- **Runner guards proven:** zero-check guard, exit-code propagation, last-summary-line-wins,
  sorted order, output-only-on-fail.
- **Check-count honesty:** all 38 scripts' observed counts reconcile to exactly 1508; every
  delta explained (loops multiply; platform arms withheld on Windows). AST sweep found no
  vacuous checks, no except-pass, no bare-assert escapes.
- **Mutation table (11 of 12 killed):**

| Mutation | Predicted catcher | Result |
|---|---|---|
| `fix.py` smuggle guard deleted | test_fix_guard 10b–10i | KILLED (6 failures, exactly the pins) |
| `_conflict_keys` reverted to first-flag-only | test_fix_guard 12k/12l/12o | KILLED (+dry-run parity 17c/17d) |
| `static_skip_reason` None on unreadable glossary | test_fix_guard 16e | KILLED |
| Variant gate disabled on union path | test_glossary_count 9f3–9f6 | KILLED (9f3) |
| Retry budget `range(7)` → `range(5)` | test_project_writes 3d/3e | KILLED (+3c, 3f, 4e) |
| Merge-branch unit warning deleted | test_glossary_count 12e | KILLED |
| Autobuild tmp sweep glob de-scoped | test_autobuild 8c/8d | KILLED |
| Docker needle `pull access denied` removed | test_epub_build infra cases | KILLED |
| Stored-recap carry disabled | test_story_recap 5a/5b | KILLED |
| `utf-8-sig` → `utf-8` in glossary.load | test_bom_tolerance 1a/1b | KILLED |
| Mock thinking simulation deleted | — | **SURVIVED → M5** |

- **Dead code:** AST sweep of all 301 top-level names in `lib/` + `migrations/`, repo-wide
  reference counts including tests: **zero** zero-reference names, zero unused imports. The
  prior "dead code: none" claim holds post-remediation.

## Doc mirror (WS2, besides M4)

Verified matching against live runs: all 20 `DEFAULTS` keys + provider temps + `version: 7`
byte-checked on disk; the full placeholder table against a captured real prompt (zero leftover
placeholders); state/manifest/sidecar/glossary-report/notes-report schemas field-by-field
against produced files; console markers verbatim; migrate DESCRIPTIONs v001–v007 and the
consent/refresh wording; init commit-subject matrix; git foreign-repo warn + `_managed`
decision matrix; exit-code table (every row triggered live except where noted); ingestion rules
against a "minefield" source directory (ASCII-only `[0-9]`, suffix sort, full-width digits
ignored); trace-log naming and the 5-calls-per-chapter shape read from a real `logs/llm-*.jsonl`.

## EPUB / cover / VCS verification highlights (WS4)

Real 3-chapter project → EPUB unzipped and inspected (mimetype first & STORED, nav+NCX, spine
follows `order` not manifest position, noteref/aside footnotes, CSS + cover embedded, CJK slug
with the documented warn); cover matrix (og:image → PIL re-encode, 404 → placeholder + flag,
garbage bytes, 20000×20000 decompression bomb, 11 MB flood, short-circuit on existing cover,
flag cleared on re-scrape); `_managed` 11/11 including the logs-only-foreign case; foreign repo
never touched (warn text verbatim, HEAD unchanged, tracked file unstaged) while the mark
mutation itself still applied; `git_commits: false` silent; generated dirs fully gitignored.

## UNVERIFIED (carried honestly)

Claims the agents could not prove on this host, reported rather than guessed: real-epubcheck
acceptance of the produced EPUB (docker untouched by policy; structural checks done by unzip);
the POSIX `os.killpg` arm of `_kill_tree`; real 300 s epubcheck timeout (handler proven by
patched raise); console-keyboard Ctrl-C delivery on Windows (exit-130 *mapping* proven through
real `main()`); `main()`'s bare-`OSError` arm via real ACL denial; `--style PATH` /
`--tags` / `--source-url` init flows; the `tn` zero-notes guard and some interrupt-path warn
strings (verified verbatim in code, not driven); real-TTY interactive "y" for template refresh
(confirm-yes pinned in-process by `test_migrate.py`); POSIX symlink invocation from
README.md:26-30.

## State

`uv run tests/run_all.py` → **38 passed, 0 failed (1508 checks)** at `0604f92`, unchanged by
this audit (read-only; this file is the only artifact). No `config.json` schema key and no
shipped template changed, so no migration is required. All agent scratch spaces lived under
the system temp directory and all mock servers (ports 8911–8915) were shut down and verified
released; one pre-existing unrelated listener on 18915 was left alone.
