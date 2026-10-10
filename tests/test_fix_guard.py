"""Tests for fix.invalid_translation_reason() and run_commands' skip guards.

`review fix` shells each report command out to the CLI where nothing would
otherwise re-check model suggestions; invalid_translation_reason() is the
in-process guard: a `glossary replace`/`glossary set` argv whose
--translation value still contains CJK for a CJK-source entry (looked up
by source or variants) must be skipped with "suggestion not in target
language", a blank value with "empty translation". The guard mirrors
review.apply_fixes(): it only fires for CJK-source entries, and never for
other verbs, missing --translation flags, or unknown sources -- the
subprocess reports those cases itself.

The --project guard: the writer never emits --project, but the report is
hand-editable, and the executor always prepends its own --project (the
GLOBAL flag) -- a smuggled `--project X` or `--project=X` token inside a
Command line would override that by argparse precedence and run the verb
against a different project dir. run_commands skips such commands
in-process (skipped-invalid, never executed -- proven by a second project
whose glossary stays byte-identical) while a clean command in the same
report still runs and the failure count stays 0 (exit semantics
unchanged).

The final case runs run_commands() end-to-end against a temp project: the
invalid spec is skipped in-process (skipped_invalid=1, no subprocess, no
glossary write) while the benign read-only spec (glossary search) runs and
applies.

The mundane cases cover the model-tier 'mundane' kind in both parser
modes: a legacy finding block (### [1] warn / mundane / 灵根) and an
explicit "- Command: glossary retire --source '灵根'" bullet both yield
the retire spec (retire is a supported verb, so the bullet is parsed,
never warned-and-ignored), and run_commands executes it as applied.

The new-format case hand-writes a report as the current writer emits it
-- YAML frontmatter, then "## Machine-applicable (apply with `review
fix`)" and "## Needs manual review" sections -- and checks the parser
needs no changes: the explicit bullet is extracted, and a command-less
variant falls back to legacy synthesis for the machine finding while
findings_count still counts findings in BOTH sections.

The noop-classification case pins the machine-marker convention:
_looks_like_noop keys ONLY on output lines whose stripped form starts with
"[glossary] noop:" (the CLI prints that prefix in every nothing-changed
path). Prose substrings no longer classify on their own -- a model-written
definition containing e.g. "nothing to do" is echoed verbatim by SUCCESS
lines ("[glossary] set 'X': definition 'old' -> 'nothing to do'") and must
count as applied -- while a successful merge with its "[git]" commit line
still must not.

The abbreviation case pins the post-allow_abbrev=False behavior: an
abbreviated --proj token inside a Command line is NOT caught by
run_commands' exact --project guard, so it reaches the child, whose
argparse (allow_abbrev=False on every parser) rejects the unknown
abbreviation with exit != 0 -- the spec counts as failed, never applied,
and the other project's glossary stays byte-identical. (Pre-fix, argparse
accepted --proj as an abbreviation of the nested parser's --project and
the command EXECUTED against the other project.)

The conflict case pins run_commands' per-invocation duplicate guard: a
second command for the same resolved (verb-target, source) -- whether it
repeats or contradicts the first, whether it names the source or a
variant that resolves to it -- is skipped in-process with its own
skipped_conflict counter and an exact console line, while different
sources never conflict.

The multi-field cases extend the conflict guard to EVERY field a command
writes: _conflict_keys returns the set of edited-field keys (replace ->
set:translation, every present `set` field flag -> its own key, merge ->
the resolved pair, retire -> the source), so `set --translation B
--definition D` occupies both keys -- it cannot slip past a queued
translation edit and rewrite the field back, and once it runs a later set
on either field is conflict-skipped (the whole command skips: none of its
fields apply). conflict_keys() is the public dry-run wrapper: the full
key set, empty for an unreadable glossary or an untracked verb.

The category cases pin static_skip_reason's --category vocabulary check:
parse_report's explicit mode only checks the verb allowlist, so a
hand-edited `glossary set --category 'TOTAL GARBAGE'` bullet used to
reach the child, exit 2 (counting failed, aborting every later finding
under --exit-on-error). It is now skipped in-process (skipped_invalid,
never executed) while a following valid spec still runs, and the check is
glossary-independent -- it fires even when glossary.json fails to parse
(invalid_translation_reason needs a readable glossary; the category check
does not).

The staleness case pins fix.report_is_stale against the writer's
glossary_digest frontmatter anchor (sha256 of glossary.json's bytes at
write time, first 12 hex chars): a fresh report is not stale, any later
glossary.json change makes it stale, reports without the field (or with
null, or BOM-prefixed, or hand-written with a matching digest) stay
runnable, and a deleted glossary.json hashes b"" -- stale against any
real digest.

The subprocess-hardening case pins run_commands' child execution: every
child gets timeout=1800, stdin=DEVNULL, and an env carrying
GIT_TERMINAL_PROMPT=0 (verified through a recording stub), and a child
that exceeds the timeout (subprocess.TimeoutExpired) counts the spec as
failed -- specs_run reflects the started child, applied/noop stay 0, the
glossary stays byte-identical, the exact "[review fix] command timed out
after 1800s: ..." line prints, and exit_on_error stops the run at the
timed-out spec.

The CLI-staleness case pins cmd_review_fix's refusal end to end: after
glossary.json changes post-generation, the real CLI `review fix` (plain
and --dry-run) exits 1 with the exact stale line and executes nothing
(disk unchanged), while --stale-ok runs (dry-run lists, real run
applies) as normal.

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    uv run tests/test_fix_guard.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0"]
# ///
import contextlib
import hashlib
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import fix, glossary, review

PASSED = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"PASS  {name}")
    else:
        FAILED.append(name)
        print(f"FAIL  {name}" + (f"  [{detail}]" if detail else ""))


def make_glossary() -> dict:
    """灵根 (variants 靈根) is CJK-source; Excalibur is not."""
    return {"terms": [
        {"source": "灵根", "variants": ["靈根"], "translation": "spirit root"},
        {"source": "Excalibur", "variants": [], "translation": "the sword"},
    ]}


def reason(argv: list[str]) -> str | None:
    return fix.invalid_translation_reason(argv, make_glossary())


def case_1_cjk_suggestion() -> None:
    """CJK suggestion on a CJK-source entry -> the target-language reason,
    for both covered verbs and both flag spellings."""
    check("1a replace: CJK --translation flagged",
          reason(["glossary", "replace", "--source", "灵根",
                  "--translation", "灵力"]) == "suggestion not in target language")
    check("1b set: CJK --translation flagged",
          reason(["glossary", "set", "--source", "灵根",
                  "--translation", "灵力"]) == "suggestion not in target language")
    check("1c mixed-script suggestion still contains CJK -> flagged",
          reason(["glossary", "replace", "--source", "灵根",
                  "--translation", "spirit 灵力"]) == "suggestion not in target language")
    check("1d '--translation=灵力' (= form) flagged",
          reason(["glossary", "replace", "--source", "灵根",
                  "--translation=灵力"]) == "suggestion not in target language")
    check("1e '--source=灵根' (= form) resolves the entry too",
          reason(["glossary", "replace", "--source=灵根",
                  "--translation", "灵力"]) == "suggestion not in target language")


def case_2_valid_suggestions() -> None:
    """English suggestions and lookup via the variant are handled."""
    check("2a English --translation -> None",
          reason(["glossary", "replace", "--source", "灵根",
                  "--translation", "spiritual root"]) is None)
    check("2b variant lookup (靈根) with CJK suggestion -> flagged",
          reason(["glossary", "replace", "--source", "靈根",
                  "--translation", "道基"]) == "suggestion not in target language")
    check("2c variant lookup with English suggestion -> None",
          reason(["glossary", "set", "--source", "靈根",
                  "--translation", "spirit essence"]) is None)


def case_3_empty_and_missing() -> None:
    """Blank --translation is its own reason; a missing --translation flag
    is not guarded (definitions may quote source text)."""
    check("3a blank --translation -> 'empty translation'",
          reason(["glossary", "replace", "--source", "灵根",
                  "--translation", "  "]) == "empty translation")
    check("3b no --translation at all (set --definition) -> None",
          reason(["glossary", "set", "--source", "灵根",
                  "--definition", "又称为灵力"]) is None)


def case_4_out_of_scope() -> None:
    """Unknown sources, non-covered verbs, and non-CJK-source entries are
    the subprocess's business, not the guard's."""
    check("4a unknown source -> None",
          reason(["glossary", "replace", "--source", "道基",
                  "--translation", "灵力"]) is None)
    check("4b glossary merge -> None",
          reason(["glossary", "merge", "--keep", "a", "--remove", "b"]) is None)
    check("4c glossary retire -> None",
          reason(["glossary", "retire", "--source", "x"]) is None)
    check("4d util replace -> None",
          reason(["util", "replace", "--source", "a",
                  "--target", "b"]) is None)
    check("4e non-CJK source (Excalibur) with CJK suggestion -> None "
          "(guard mirrors apply_fixes: CJK-source entries only)",
          reason(["glossary", "replace", "--source", "Excalibur",
                  "--translation", "灵力"]) is None)


def case_5_run_commands_integration() -> None:
    """run_commands skips the invalid spec in-process (no subprocess, no
    glossary write) and still runs the benign read-only spec."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        glossary.save(root, {"terms": [
            {"source": "灵根", "variants": [], "translation": "spirit root"},
        ]})
        before = (root / "glossary.json").read_bytes()
        specs = [
            fix.CommandSpec(
                raw="glossary replace --source 灵根 --translation 灵力",
                argv=["glossary", "replace", "--source", "灵根",
                      "--translation", "灵力"],
                line_no=1, finding={}),
            fix.CommandSpec(
                raw="glossary search 灵根",
                argv=["glossary", "search", "灵根"],
                line_no=2, finding={}),
        ]
        result = fix.run_commands(root, SCRIPTS / "translate.py", specs)
        check("5a run_commands: invalid spec skipped (skipped_invalid=1)",
              result["skipped_invalid"] == 1, f"result={result}")
        check("5b run_commands: exactly one subprocess started (specs_run=1)",
              result["specs_run"] == 1, f"result={result}")
        check("5c run_commands: benign spec succeeded (applied 1, failed 0)",
              result["applied"] == 1 and result["failed"] == 0,
              f"result={result}")
        check("5d run_commands: glossary.json byte-unchanged",
              (root / "glossary.json").read_bytes() == before)


def case_6_mundane_report_parsing() -> None:
    """Both parser modes map a mundane finding to `glossary retire`: legacy
    synthesis from the finding block, and the explicit (shlex-quoted)
    Command bullet -- a supported verb, so parsed, not warned-and-ignored."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        report = root / "review-report.md"

        report.write_text(
            "# Glossary Review Report\n"
            "\n"
            "### [1] warn / mundane / 灵根\n"
            "\n"
            "- Reason: ordinary word, not a novel-specific term\n"
            "- Tier: model\n",
            encoding="utf-8",
        )
        specs, count = fix.parse_report(report)
        check("6a legacy mundane: one finding, one synthesized command",
              count == 1 and len(specs) == 1,
              f"count={count} specs={len(specs)}")
        check("6b legacy mundane: synthesized spec retires the CJK source",
              bool(specs) and specs[0].argv == ["glossary", "retire",
                                                "--source", "灵根"]
              and specs[0].line_no == 0,
              f"argv={specs[0].argv if specs else None}")

        report.write_text(
            "- Command: glossary retire --source '灵根'\n",
            encoding="utf-8",
        )
        specs, count = fix.parse_report(report)
        check("6c explicit mundane: retire bullet parsed as a supported verb",
              len(specs) == 1
              and specs[0].argv == ["glossary", "retire", "--source", "灵根"]
              and specs[0].line_no == 1
              and specs[0].raw == "glossary retire --source '灵根'",
              f"specs={[s.argv for s in specs]}")


def case_7_mundane_retire_run() -> None:
    """run_commands executes the parsed retire spec as applied (guard not
    applicable to retire, exit 0, not a noop) and the entry is retired
    on disk."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        glossary.save(root, {"terms": [
            {"source": "灵根", "variants": [], "translation": "spirit root"},
        ]})
        report = root / "review-report.md"
        report.write_text(
            "- Command: glossary retire --source '灵根'\n",
            encoding="utf-8",
        )
        specs, _count = fix.parse_report(report)
        result = fix.run_commands(root, SCRIPTS / "translate.py", specs)
        check("7a run mundane: retire ran and counted as applied",
              result["specs_run"] == 1 and result["applied"] == 1
              and result["failed"] == 0 and result["noop"] == 0
              and result["skipped_invalid"] == 0, f"result={result}")
        g = glossary.load(root)
        check("7b run mundane: entry removed, 灵根 recorded in 'retired'",
              g.get("terms") == [] and g.get("retired") == ["灵根"],
              f"terms={g.get('terms')} retired={g.get('retired')}")


def case_8_new_format_report_parsing() -> None:
    """A report in the writer's current format (YAML frontmatter, then the
    Machine-applicable / Needs manual review split) parses unchanged in both
    modes: the explicit Command bullet is extracted verbatim, and a
    command-less variant synthesizes the machine finding's command via
    legacy synthesis while findings_count counts findings in BOTH sections
    (the manual finding itself yields no spec -- command_for_finding -> None)."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        report = root / "review-report.md"

        frontmatter = (
            "---\n"
            "report_type: glossary-review\n"
            "generated: 2026-01-01T00:00:00+00:00\n"
            "generated_by: review glossary\n"
            "source_lang: zh\n"
            "target_lang: en\n"
            "entries_reviewed: 2\n"
            "batch_errors: 0\n"
            "outcome:\n"
            "  warn: 1\n"
            "  info: 1\n"
            "machine_applicable: 1\n"
            "manual_review: 1\n"
            "manual_review_indices: [2]\n"
            "---\n"
            "\n"
            "# Glossary Review Report\n"
            "\n"
        )
        body = (
            "## Machine-applicable (apply with `review fix`)\n"
            "\n"
            "### [1] warn / mistranslation / 灵根\n"
            "\n"
            "- Reason: wrong rendering of the term\n"
            "- Suggestion: spiritual root\n"
            "- Tier: model\n"
            '- Action: Set the `translation` field to "spiritual root".\n'
            "- Command: glossary replace --source '灵根' "
            "--translation 'spiritual root'\n"
            "\n"
            "## Needs manual review (decide yourself or hand to an agent)\n"
            "\n"
            "### [2] info / variant / 天雷宗\n"
            "\n"
            "- Reason: model flagged the variant for a judgment call\n"
            "- Tier: model\n"
            "- Action: Decide what to do with the flagged variant.\n"
            "\n"
        )
        full_text = frontmatter + body
        command_line = ("- Command: glossary replace --source '灵根' "
                        "--translation 'spiritual root'")

        report.write_text(full_text, encoding="utf-8")
        specs, count = fix.parse_report(report)
        check("8a new format: findings counted across BOTH sections",
              count == 2, f"count={count}")
        check("8b new format: explicit Command bullet extracted (argv + line no)",
              len(specs) == 1
              and specs[0].argv == ["glossary", "replace", "--source", "灵根",
                                    "--translation", "spiritual root"]
              and specs[0].line_no
              == full_text.splitlines().index(command_line) + 1,
              f"specs={[(s.argv, s.line_no) for s in specs]}")

        report.write_text(
            "\n".join(ln for ln in full_text.splitlines()
                      if not ln.startswith("- Command: ")) + "\n",
            encoding="utf-8",
        )
        specs, count = fix.parse_report(report)
        check("8c new format: command-less variant synthesizes the machine "
              "command, count still 2",
              count == 2 and len(specs) == 1
              and specs[0].argv == ["glossary", "replace", "--source", "灵根",
                                    "--translation", "spiritual root"]
              and specs[0].line_no == 0,
              f"count={count} specs={[(s.argv, s.line_no) for s in specs]}")


def case_9_noop_classification() -> None:
    """_looks_like_noop keys ONLY on the machine no-op marker: any output
    line whose stripped form starts with "[glossary] noop:" buckets a
    successful exit as a no-op. Prose substrings ("nothing to do",
    "already up-to-date", ...) no longer classify on their own -- a
    model-written definition containing them is echoed verbatim by SUCCESS
    lines (e.g. "[glossary] set 'X': definition 'old' -> 'nothing to do'")
    and must count as applied. The successful-merge regression stays: the
    "[git]" commit line naming the merge never classified and still must
    not."""
    noop_out = "[glossary] noop: set '灵根': already up-to-date\n"
    check("9a noop: the machine marker line classifies as a no-op",
          fix._looks_like_noop(noop_out) is True, f"out={noop_out!r}")
    check("9b noop: marker detection is line-anchored and strip-tolerant",
          fix._looks_like_noop(
              "warning: something\n"
              "  [glossary] noop: merge: '灵石' already retired\n") is True,
          "")
    hostile = "[glossary] set '灵根': definition 'old' -> 'nothing to do'\n"
    check("9c noop: 'nothing to do' inside a SUCCESS line is applied, "
          "not a no-op",
          fix._looks_like_noop(hostile) is False, f"out={hostile!r}")
    merge_ok = (
        "[glossary] merged '灵石' into '灵砂' (variants +1, alt +0)\n"
        "[git] committed abc1234 glossary merge: '灵石' into '灵砂'\n"
    )
    check("9d noop: successful merge output (with its [git] subject) is "
          "NOT a no-op",
          fix._looks_like_noop(merge_ok) is False, f"out={merge_ok!r}")
    check("9e noop: a definition echoing 'already up-to-date' is applied",
          fix._looks_like_noop(
              "[glossary] set '灵根': definition 'x' -> 'already up-to-date'\n")
          is False, "")


def case_10_project_override_guard() -> None:
    """A smuggled --project (either spelling) inside a Command line is
    skipped in-process, never executed -- proven by a second project whose
    glossary stays byte-identical -- while the clean command in the same
    report still runs; failed stays 0, so the CLI's exit semantics are
    unchanged by the skips."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        glossary.save(root, {"terms": [
            {"source": "灵根", "variants": [], "translation": "spirit root"},
        ]})
        other = root / "elsewhere"
        other.mkdir()
        glossary.save(other, {"terms": [
            {"source": "灵根", "variants": [], "translation": "spirit root"},
        ]})
        other_before = (other / "glossary.json").read_bytes()
        other_flag = other.as_posix()

        report = root / "review-report.md"
        report.write_text(
            f"- Command: glossary replace --source '灵根' "
            f"--translation 'spiritual root' --project {other_flag}\n"
            f"- Command: glossary replace --source '灵根' "
            f"--translation 'new root' --project={other_flag}\n"
            "- Command: glossary set --source '灵根' "
            "--definition 'A glossary term.'\n",
            encoding="utf-8",
        )
        specs, _count = fix.parse_report(report)
        check("10a guard: all three bullets parse (the smuggled tokens "
              "ride along in argv)",
              len(specs) == 3
              and specs[0].argv[-2:] == ["--project", other_flag]
              and specs[1].argv[-1] == f"--project={other_flag}",
              f"argvs={[s.argv for s in specs]}")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = fix.run_commands(root, SCRIPTS / "translate.py", specs)
        out = buf.getvalue()
        check("10b guard: both smuggled commands skipped (skipped_invalid=2)",
              result["skipped_invalid"] == 2, f"result={result}")
        check("10c guard: only the clean command executed (specs_run=1)",
              result["specs_run"] == 1, f"result={result}")
        check("10d guard: clean command applied, nothing failed (exit "
              "semantics unchanged)",
              result["applied"] == 1 and result["failed"] == 0,
              f"result={result}")
        expected_1 = (f"[review fix] skipped [1]: command overrides --project "
                      f"(glossary replace --source '灵根' "
                      f"--translation 'spiritual root' --project {other_flag})")
        expected_2 = (f"[review fix] skipped [2]: command overrides --project "
                      f"(glossary replace --source '灵根' "
                      f"--translation 'new root' --project={other_flag})")
        check("10e guard: exact skip line for the '--project X' spelling",
              expected_1 in out, f"out={out!r}")
        check("10f guard: exact skip line for the '--project=X' spelling",
              expected_2 in out, f"out={out!r}")
        check("10g guard: exactly two override skips printed",
              out.count("command overrides --project") == 2, f"out={out!r}")

        entry = glossary.load(root)["terms"][0]
        check("10h guard: the project's translation untouched (replace "
              "never ran), the clean set applied",
              entry.get("translation") == "spirit root"
              and entry.get("definition") == "A glossary term.",
              f"entry={entry}")
        check("10i guard: the smuggled target project byte-unchanged",
              (other / "glossary.json").read_bytes() == other_before, "")


def case_11_argparse_abbreviation() -> None:
    """An abbreviated --proj token is NOT caught by run_commands' exact
    --project guard, so it reaches the child -- whose argparse (built with
    allow_abbrev=False on every parser) rejects the unknown abbreviation
    with exit != 0. Each spec therefore counts as failed, never applied,
    and the other project's glossary stays byte-identical: the smuggled
    target is never written. (Before allow_abbrev=False, argparse accepted
    --proj as an abbreviation of the nested parser's --project and the
    command EXECUTED against the other project -- this case pins the
    post-fix behavior.) The two commands use different sources so the
    conflict guard never engages: the failure must come from the child."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        glossary.save(root, {"terms": [
            {"source": "灵根", "variants": [], "translation": "spirit root"},
            {"source": "Excalibur", "variants": [], "translation": "the sword"},
        ]})
        other = root / "elsewhere"
        other.mkdir()
        glossary.save(other, {"terms": [
            {"source": "灵根", "variants": [], "translation": "spirit root"},
        ]})
        (other / "chapters.json").write_text("[]\n", encoding="utf-8")
        other_before = (other / "glossary.json").read_bytes()
        primary_before = (root / "glossary.json").read_bytes()
        other_flag = other.as_posix()

        report = root / "review-report.md"
        report.write_text(
            f"- Command: glossary replace --source '灵根' "
            f"--translation 'spiritual root' --proj {other_flag}\n"
            f"- Command: glossary replace --source 'Excalibur' "
            f"--translation 'the true sword' --proj={other_flag}\n",
            encoding="utf-8",
        )
        specs, _count = fix.parse_report(report)
        check("11a abbrev: both abbreviated bullets parse (the in-process "
              "guard only matches exact --project spellings)",
              len(specs) == 2, f"argvs={[s.argv for s in specs]}")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = fix.run_commands(root, SCRIPTS / "translate.py", specs)
        out = buf.getvalue()
        check("11b abbrev: neither command skipped in-process",
              result["skipped_invalid"] == 0
              and result["skipped_conflict"] == 0, f"result={result}")
        check("11c abbrev: both reached the child and were rejected "
              "(failed=2, specs_run=2)",
              result["failed"] == 2 and result["specs_run"] == 2,
              f"result={result}")
        check("11d abbrev: neither counted as applied or no-op",
              result["applied"] == 0 and result["noop"] == 0,
              f"result={result}")
        check("11e abbrev: exact failed-at lines for both spellings",
              "[review fix] failed at [1]" in out
              and "[review fix] failed at [2]" in out, f"out={out!r}")
        check("11f abbrev: the other project's glossary byte-unchanged",
              (other / "glossary.json").read_bytes() == other_before, "")
        check("11g abbrev: the primary project's glossary byte-unchanged",
              (root / "glossary.json").read_bytes() == primary_before, "")


def case_12_conflict_guard() -> None:
    """run_commands' per-invocation conflict guard: a second command for the
    same resolved (verb-target, source) is skipped in-process -- whether it
    contradicts or repeats the first, and whether it names the source or a
    variant that resolves to it -- with its own skipped_conflict counter
    (never merged into skipped_invalid) and an exact console line naming
    the RESOLVED source. Different sources never conflict."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        glossary.save(root, {"terms": [
            {"source": "灵根", "variants": ["靈根"],
             "translation": "spirit root"},
        ]})
        (root / "chapters.json").write_text("[]\n", encoding="utf-8")
        report = root / "review-report.md"
        report.write_text(
            "- Command: glossary replace --source '灵根' "
            "--translation 'spiritual root'\n"
            "- Command: glossary replace --source '靈根' "
            "--translation 'another root'\n",
            encoding="utf-8",
        )
        specs, _count = fix.parse_report(report)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = fix.run_commands(root, SCRIPTS / "translate.py", specs)
        out = buf.getvalue()
        check("12a conflict: first applied, nothing failed (specs_run=1)",
              result["applied"] == 1 and result["failed"] == 0
              and result["specs_run"] == 1, f"result={result}")
        check("12b conflict: own counter, not skipped_invalid",
              result["skipped_conflict"] == 1
              and result["skipped_invalid"] == 0, f"result={result}")
        check("12c conflict: exact skip line naming the RESOLVED canonical "
              "source (the command named the variant 靈根)",
              "[review fix] skipped [2]: conflicting command for "
              "'灵根' (already queued)" in out, f"out={out!r}")
        entry = glossary.load(root)["terms"][0]
        check("12d conflict: the contradicting command never ran (disk "
              "keeps the first translation)",
              entry.get("translation") == "spiritual root", f"entry={entry}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        glossary.save(root, {"terms": [
            {"source": "灵根", "variants": ["靈根"],
             "translation": "spirit root"},
        ]})
        (root / "chapters.json").write_text("[]\n", encoding="utf-8")
        report = root / "review-report.md"
        report.write_text(
            "- Command: glossary replace --source '灵根' "
            "--translation 'spiritual root'\n"
            "- Command: glossary replace --source '灵根' "
            "--translation 'spiritual root'\n",
            encoding="utf-8",
        )
        specs, _count = fix.parse_report(report)
        result = fix.run_commands(root, SCRIPTS / "translate.py", specs)
        check("12e conflict: identical duplicate skipped the same way "
              "(applied=1, conflict=1, noop=0)",
              result["applied"] == 1 and result["skipped_conflict"] == 1
              and result["noop"] == 0 and result["specs_run"] == 1,
              f"result={result}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        glossary.save(root, {"terms": [
            {"source": "灵根", "variants": [], "translation": "spirit root"},
            {"source": "Excalibur", "variants": [], "translation": "the sword"},
        ]})
        (root / "chapters.json").write_text("[]\n", encoding="utf-8")
        report = root / "review-report.md"
        report.write_text(
            "- Command: glossary replace --source '灵根' "
            "--translation 'spiritual root'\n"
            "- Command: glossary replace --source 'Excalibur' "
            "--translation 'the true sword'\n",
            encoding="utf-8",
        )
        specs, _count = fix.parse_report(report)
        result = fix.run_commands(root, SCRIPTS / "translate.py", specs)
        check("12f conflict: different sources both apply (conflict=0)",
              result["applied"] == 2 and result["skipped_conflict"] == 0
              and result["failed"] == 0, f"result={result}")
        terms = {e["source"]: e for e in glossary.load(root)["terms"]}
        check("12g conflict: both translations updated on disk",
              terms["灵根"]["translation"] == "spiritual root"
              and terms["Excalibur"]["translation"] == "the true sword",
              f"terms={terms}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        glossary.save(root, {"terms": [
            {"source": "master", "translation": "the original"},
            {"source": "dupA", "translation": "the original"},
            {"source": "dupB", "translation": "the original"},
        ]})
        (root / "chapters.json").write_text("[]\n", encoding="utf-8")
        lines = [
            "- Command: glossary merge --keep 'master' --remove 'dupA'",
            "- Command: glossary merge --keep 'master' --remove 'dupB'",
        ]
        report = root / "review-report.md"
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        specs, _count = fix.parse_report(report)
        result = fix.run_commands(root, SCRIPTS / "translate.py", specs)
        terms = {e["source"] for e in glossary.load(root)["terms"]}
        check("12h merge-pair: two merges into one keeper BOTH run",
              result["applied"] == 2 and result["skipped_conflict"] == 0
              and "dupA" not in terms and "dupB" not in terms,
              f"result={result} terms={terms}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        glossary.save(root, {"terms": [
            {"source": "灵根", "translation": "spirit root",
             "definition": "the root of spirit"},
        ]})
        (root / "chapters.json").write_text("[]\n", encoding="utf-8")
        lines = [
            "- Command: glossary replace --source '灵根' "
            "--translation 'spiritual root'",
            "- Command: glossary set --source '灵根' --translation 'other'",
        ]
        report = root / "review-report.md"
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        specs, _count = fix.parse_report(report)
        result = fix.run_commands(root, SCRIPTS / "translate.py", specs)
        entry = glossary.load(root)["terms"][0]
        check("12i conflict-key: replace and set --translation collide "
              "(second skipped, disk keeps the first)",
              result["applied"] == 1 and result["skipped_conflict"] == 1
              and entry.get("translation") == "spiritual root",
              f"result={result} entry={entry}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        glossary.save(root, {"terms": [
            {"source": "灵根", "translation": "spirit root",
             "definition": "the root of spirit"},
        ]})
        (root / "chapters.json").write_text("[]\n", encoding="utf-8")
        lines = [
            "- Command: glossary set --source '灵根' "
            "--definition 'a better definition'",
            "- Command: glossary set --source '灵根' --category 'skill'",
        ]
        report = root / "review-report.md"
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        specs, _count = fix.parse_report(report)
        result = fix.run_commands(root, SCRIPTS / "translate.py", specs)
        entry = glossary.load(root)["terms"][0]
        check("12j conflict-key: two sets on DIFFERENT fields both run",
              result["applied"] == 2 and result["skipped_conflict"] == 0
              and entry.get("definition") == "a better definition"
              and entry.get("category") == "skill",
              f"result={result} entry={entry}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        glossary.save(root, {"terms": [
            {"source": "灵根", "translation": "spirit root",
             "definition": "the root of spirit"},
        ]})
        (root / "chapters.json").write_text("[]\n", encoding="utf-8")
        lines = [
            "- Command: glossary replace --source '灵根' "
            "--translation 'spiritual root'",
            "- Command: glossary set --source '灵根' "
            "--translation 'B' --definition 'D'",
        ]
        report = root / "review-report.md"
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        specs, _count = fix.parse_report(report)
        result = fix.run_commands(root, SCRIPTS / "translate.py", specs)
        entry = glossary.load(root)["terms"][0]
        check("12k conflict-key: multi-field set collides on the queued "
              "translation field (replace applied, set skipped)",
              result["applied"] == 1 and result["skipped_conflict"] == 1
              and result["failed"] == 0,
              f"result={result}")
        check("12l conflict-key: the skipped set applied NEITHER field "
              "(translation stays the replace's, no definition written)",
              entry.get("translation") == "spiritual root"
              and entry.get("definition") == "the root of spirit",
              f"entry={entry}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        glossary.save(root, {"terms": [
            {"source": "灵根", "translation": "spirit root",
             "definition": "the root of spirit"},
        ]})
        (root / "chapters.json").write_text("[]\n", encoding="utf-8")
        lines = [
            "- Command: glossary set --source '灵根' "
            "--translation 'B' --definition 'D'",
            "- Command: glossary set --source '灵根' --definition 'Z'",
        ]
        report = root / "review-report.md"
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        specs, _count = fix.parse_report(report)
        result = fix.run_commands(root, SCRIPTS / "translate.py", specs)
        entry = glossary.load(root)["terms"][0]
        check("12m conflict-key: multi-field set first claims both fields -- "
              "a later set on one of them is skipped, disk keeps the first",
              result["applied"] == 1 and result["skipped_conflict"] == 1
              and entry.get("translation") == "B"
              and entry.get("definition") == "D",
              f"result={result} entry={entry}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        glossary.save(root, {"terms": [
            {"source": "灵根", "translation": "spirit root",
             "definition": "the root of spirit"},
        ]})
        (root / "chapters.json").write_text("[]\n", encoding="utf-8")
        report = root / "review-report.md"
        report.write_text(
            "- Command: glossary set --source '灵根' "
            "--translation 'B' --definition 'D'\n",
            encoding="utf-8",
        )
        specs, _count = fix.parse_report(report)
        result = fix.run_commands(root, SCRIPTS / "translate.py", specs)
        entry = glossary.load(root)["terms"][0]
        check("12n conflict-key: a lone multi-field set applies BOTH fields",
              result["applied"] == 1 and result["skipped_conflict"] == 0
              and entry.get("translation") == "B"
              and entry.get("definition") == "D",
              f"result={result} entry={entry}")

    g = make_glossary()
    check("12o conflict_keys: a multi-field set yields one key per written "
          "field, sharing the resolved source",
          fix.conflict_keys(
              ["glossary", "set", "--source", "灵根",
               "--translation", "B", "--definition", "D"], g)
          == {("set:translation", "灵根"), ("set:definition", "灵根")},
          "")
    check("12p conflict_keys: empty for an unreadable glossary (None) and "
          "an untracked verb",
          fix.conflict_keys(
              ["glossary", "set", "--source", "灵根", "--translation", "B"],
              None) == set()
          and fix.conflict_keys(["glossary", "search", "灵根"], g) == set(),
          "")


def case_13_report_staleness() -> None:
    """report_is_stale + the writer's glossary_digest frontmatter anchor
    (sha256 of glossary.json's bytes at write time, first 12 hex chars): a
    freshly written report is not stale, any later glossary.json change
    makes it stale, and reports without the field (BOM-prefixed or plain),
    with an explicit null, or hand-written with a matching digest stay
    runnable. A deleted glossary.json hashes b"" -- stale against any real
    digest."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        glossary.save(root, {"terms": [
            {"source": "灵根", "variants": [], "translation": "spirit root"},
        ]})
        path = review.write_report(
            root, findings=[], terms=[], applied=[], skipped=[],
            ran_fix=False, batches=0, batch_errors=[],
            cfg={"source_lang": "zh", "target_lang": "en"},
        )
        digest = hashlib.sha256(
            (root / "glossary.json").read_bytes()
        ).hexdigest()[:12]
        text = path.read_text(encoding="utf-8")
        check("13a stale: writer stamps glossary_digest with the current "
              "file's sha256[:12]",
              f"glossary_digest: {digest}\n" in text, f"text={text!r}")
        check("13b stale: freshly written report is not stale",
              fix.report_is_stale(root, path) is False, "")

        glossary.save(root, {"terms": [
            {"source": "灵根", "variants": [], "translation": "new root"},
        ]})
        check("13c stale: stale after glossary.json changed",
              fix.report_is_stale(root, path) is True, "")

        hand = root / "hand-written.md"
        hand.write_text(
            "---\nreport_type: glossary-review\n---\n\n# report\n",
            encoding="utf-8-sig",
        )
        check("13d stale: BOM'd report without the field is not stale",
              fix.report_is_stale(root, hand) is False, "")
        nullfm = root / "null-digest.md"
        nullfm.write_text(
            "---\nreport_type: glossary-review\nglossary_digest: null\n"
            "---\n\n# report\n",
            encoding="utf-8",
        )
        check("13e stale: 'glossary_digest: null' is not stale",
              fix.report_is_stale(root, nullfm) is False, "")
        good = hashlib.sha256(
            (root / "glossary.json").read_bytes()
        ).hexdigest()[:12]
        okfm = root / "matching-digest.md"
        okfm.write_text(
            f"---\nglossary_digest: {good}\n---\n\n# report\n",
            encoding="utf-8",
        )
        check("13f stale: hand-written report with the matching digest is "
              "not stale (checker computes over bytes)",
              fix.report_is_stale(root, okfm) is False, "")

        (root / "glossary.json").unlink()
        check("13g stale: deleted glossary.json counts as stale "
              "(b\"\" digest disagrees)",
              fix.report_is_stale(root, path) is True, "")


def case_14_subprocess_timeout() -> None:
    """run_commands' child hardening: every child gets timeout=1800,
    stdin=DEVNULL, and an env carrying GIT_TERMINAL_PROMPT=0 (pinned via a
    recording stub swapped in for fix.subprocess, per the suite's
    attribute-swap convention), and a child that exceeds the timeout
    (subprocess.TimeoutExpired) counts the spec as failed -- specs_run
    reflects the started child, applied/noop stay 0, the glossary stays
    byte-identical, the exact timed-out line prints, and exit_on_error
    stops the run at the timed-out spec exactly like the rc!=0 path."""
    specs = [
        fix.CommandSpec(
            raw="glossary retire --source '灵根'",
            argv=["glossary", "retire", "--source", "灵根"],
            line_no=1, finding={}),
        fix.CommandSpec(
            raw="glossary retire --source 'Excalibur'",
            argv=["glossary", "retire", "--source", "Excalibur"],
            line_no=2, finding={}),
    ]

    def make_project(root: Path) -> bytes:
        glossary.save(root, {"terms": [
            {"source": "灵根", "variants": [], "translation": "spirit root"},
            {"source": "Excalibur", "variants": [], "translation": "the sword"},
        ]})
        (root / "chapters.json").write_text("[]\n", encoding="utf-8")
        return (root / "glossary.json").read_bytes()

    def stub(run_fn) -> SimpleNamespace:
        return SimpleNamespace(
            run=run_fn,
            DEVNULL=subprocess.DEVNULL,
            TimeoutExpired=subprocess.TimeoutExpired,
        )

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        before = make_project(root)
        seen_kwargs: dict = {}

        def hanging_run(cmd, **kwargs):
            seen_kwargs.update(kwargs)
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1800)

        orig_subprocess = fix.subprocess
        fix.subprocess = stub(hanging_run)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                result = fix.run_commands(
                    root, SCRIPTS / "translate.py", specs[:1])
            out = buf.getvalue()
        finally:
            fix.subprocess = orig_subprocess
        check("14a timeout: spec failed, never applied/no-op",
              result["failed"] == 1 and result["applied"] == 0
              and result["noop"] == 0, f"result={result}")
        check("14b timeout: the started child counts in specs_run",
              result["specs_run"] == 1, f"result={result}")
        check("14c timeout: exact line",
              out == "[review fix] command timed out after 1800s: glossary "
                     "retire --source '灵根'\n",
              f"out={out!r}")
        check("14d timeout: child got timeout=1800, stdin=DEVNULL, "
              "GIT_TERMINAL_PROMPT=0 in an inherited env",
              seen_kwargs.get("timeout") == 1800
              and seen_kwargs.get("stdin") is subprocess.DEVNULL
              and seen_kwargs.get("env", {}).get("GIT_TERMINAL_PROMPT") == "0"
              and seen_kwargs.get("env", {}).get("PATH")
              == os.environ.get("PATH"),
              f"kwargs={sorted(seen_kwargs)}")
        check("14e timeout: glossary byte-unchanged",
              (root / "glossary.json").read_bytes() == before, "")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        before = make_project(root)

        def hanging_run2(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1800)

        orig_subprocess = fix.subprocess
        fix.subprocess = stub(hanging_run2)
        try:
            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                result2 = fix.run_commands(
                    root, SCRIPTS / "translate.py", specs, exit_on_error=True)
            out2 = buf2.getvalue()
        finally:
            fix.subprocess = orig_subprocess
        check("14f timeout: exit_on_error breaks after the timed-out spec",
              result2["specs_run"] == 1 and result2["failed"] == 1,
              f"result={result2}")
        check("14g timeout: exit_on_error keeps applied/noop at 0",
              result2["applied"] == 0 and result2["noop"] == 0,
              f"result={result2}")
        check("14h timeout: glossary byte-unchanged under exit_on_error",
              (root / "glossary.json").read_bytes() == before, "")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        make_project(root)
        orig_subprocess = fix.subprocess
        fix.subprocess = stub(hanging_run2)
        try:
            result3 = fix.run_commands(root, SCRIPTS / "translate.py", specs)
        finally:
            fix.subprocess = orig_subprocess
        check("14i timeout: continue-past runs every spec (specs_run=2, "
              "failed=2)",
              result3["specs_run"] == 2 and result3["failed"] == 2
              and result3["applied"] == 0, f"result={result3}")


def case_15_staleness_cli_refusal() -> None:
    """cmd_review_fix's staleness gate end to end through the real CLI
    (same subprocess style as test_main_exit_codes): a writer-generated
    report whose glossary.json changed since generation is refused -- exit
    1, the exact stale line, and NO command executed (disk unchanged),
    --dry-run refusing identically -- while --stale-ok runs as normal
    (dry-run lists the command, the real run applies it)."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        glossary.save(root, {"terms": [
            {"source": "灵根", "variants": [], "translation": "spirit root"},
        ]})
        (root / "chapters.json").write_text("[]\n", encoding="utf-8")
        report = review.write_report(
            root, findings=[], terms=[], applied=[], skipped=[],
            ran_fix=False, batches=0, batch_errors=[],
            cfg={"source_lang": "zh", "target_lang": "en"},
        )
        with report.open("a", encoding="utf-8") as fh:
            fh.write("- Command: glossary set --source '灵根' "
                     "--definition 'A glossary term.'\n")

        def run_fix(*flags: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                [sys.executable, str(SCRIPTS / "translate.py"),
                 "review", "fix", "--project", str(root), *flags],
                capture_output=True, text=True, check=False,
                encoding="utf-8", errors="replace", timeout=300,
            )

        glossary.save(root, {"terms": [
            {"source": "灵根", "variants": [], "translation": "new root"},
        ]})
        stale_bytes = (root / "glossary.json").read_bytes()

        proc = run_fix()
        check("15a stale CLI: plain run exits 1", proc.returncode == 1,
              f"rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}")
        check("15b stale CLI: exact refusal line",
              proc.stdout == "[review fix] report is stale (glossary "
                             "changed since generation) - regenerate with "
                             "review glossary\n",
              f"out={proc.stdout!r}")
        check("15c stale CLI: no command executed (disk unchanged)",
              (root / "glossary.json").read_bytes() == stale_bytes, "")

        proc = run_fix("--dry-run")
        check("15d stale CLI: --dry-run refuses identically",
              proc.returncode == 1
              and proc.stdout == "[review fix] report is stale (glossary "
                                 "changed since generation) - regenerate "
                                 "with review glossary\n",
              f"rc={proc.returncode} out={proc.stdout!r}")
        check("15e stale CLI: the dry-run refusal executed nothing",
              (root / "glossary.json").read_bytes() == stale_bytes, "")

        proc = run_fix("--dry-run", "--stale-ok")
        check("15f stale-ok: dry-run lists the command and exits 0",
              proc.returncode == 0 and "[review fix] [1] " in proc.stdout,
              f"rc={proc.returncode} out={proc.stdout!r}")
        check("15g stale-ok: the listing dry-run still wrote nothing",
              (root / "glossary.json").read_bytes() == stale_bytes, "")

        proc = run_fix("--stale-ok")
        check("15h stale-ok: the real run applies and exits 0",
              proc.returncode == 0 and "[review fix] applied 1" in proc.stdout,
              f"rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}")
        g = glossary.load(root)
        check("15i stale-ok: the command really ran (definition written)",
              g["terms"][0].get("definition") == "A glossary term.",
              f"terms={g['terms']}")


def case_16_category_guard() -> None:
    """static_skip_reason's --category vocabulary check closes the
    hand-edit vector: parse_report's explicit mode only checks the verb
    allowlist, so a hand-edited off-vocabulary `glossary set --category`
    bullet used to reach the child, exit 2 (counting failed -- aborting
    every later finding under --exit-on-error). It is now skipped
    in-process (skipped_invalid, never executed -- the entry on disk stays
    untouched) while a following valid spec still runs, and the check is
    glossary-independent: it also fires when glossary.json fails to parse,
    where invalid_translation_reason has nothing readable to check."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        glossary.save(root, {"terms": [
            {"source": "灵根", "variants": [], "translation": "spirit root"},
            {"source": "Excalibur", "variants": [], "translation": "the sword"},
        ]})
        report = root / "review-report.md"
        report.write_text(
            "- Command: glossary set --source '灵根' "
            "--category 'TOTAL GARBAGE' --translation 'ok'\n"
            "- Command: glossary set --source 'Excalibur' "
            "--definition 'the true sword'\n",
            encoding="utf-8",
        )
        specs, _count = fix.parse_report(report)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = fix.run_commands(root, SCRIPTS / "translate.py", specs)
        out = buf.getvalue()
        check("16a category: the off-vocabulary bullet counts under "
              "skipped_invalid",
              result["skipped_invalid"] == 1
              and result["skipped_conflict"] == 0, f"result={result}")
        check("16b category: never executed, nothing failed, the valid "
              "spec after it still ran (no abort)",
              result["specs_run"] == 1 and result["failed"] == 0
              and result["applied"] == 1, f"result={result}")
        check("16c category: exact skip reason with the vocabulary hint",
              "[review fix] skipped [1]: unknown category 'TOTAL GARBAGE' "
              "(must be one of:" in out, f"out={out!r}")
        terms = {e["source"]: e for e in glossary.load(root)["terms"]}
        check("16d category: 灵根 untouched on disk (neither the category "
              "nor the translation landed), the valid definition applied",
              terms["灵根"].get("category") is None
              and terms["灵根"].get("translation") == "spirit root"
              and terms["Excalibur"].get("definition") == "the true sword",
              f"terms={terms}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "glossary.json").write_text("[1, 2]\n", encoding="utf-8")
        report = root / "review-report.md"
        report.write_text(
            "- Command: glossary set --source '灵根' "
            "--category 'TOTAL GARBAGE' --translation 'ok'\n",
            encoding="utf-8",
        )
        specs, _count = fix.parse_report(report)
        result = fix.run_commands(root, SCRIPTS / "translate.py", specs)
        check("16e category: fires with an unreadable glossary too "
              "(skipped_invalid, never executed, nothing failed)",
              result["skipped_invalid"] == 1 and result["specs_run"] == 0
              and result["failed"] == 0, f"result={result}")
        check("16f category: the parse-broken glossary.json byte-unchanged",
              (root / "glossary.json").read_text(encoding="utf-8")
              == "[1, 2]\n", "")


def case_17_dryrun_skip_parity() -> None:
    """review fix --dry-run must annotate every spec the real run would
    skip, so the preview can never list a doomed command as runnable: the
    --project smuggle guard (run_commands refuses such specs before any
    other check) and the conflict guard (a later command conflicting on a
    claimed field key) both surface as SKIP lines, and the summary counts
    them under 'would be skipped (invalid or conflicting)'."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        glossary.save(root, {"terms": [
            {"source": "灵根", "variants": [], "translation": "spirit root"},
        ]})
        (root / "chapters.json").write_text("[]\n", encoding="utf-8")
        before = (root / "glossary.json").read_bytes()
        report = review.write_report(
            root, findings=[], terms=[], applied=[], skipped=[],
            ran_fix=False, batches=0, batch_errors=[],
            cfg={"source_lang": "zh", "target_lang": "en"},
        )
        with report.open("a", encoding="utf-8") as fh:
            fh.write("- Command: glossary replace --source '灵根' "
                     "--translation 'x' --project /elsewhere\n")
            fh.write("- Command: glossary replace --source '灵根' "
                     "--translation 'first'\n")
            fh.write("- Command: glossary set --source '灵根' "
                     "--translation 'second' --definition 'D.'\n")

        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "translate.py"),
             "review", "fix", "--project", str(root), "--dry-run"],
            capture_output=True, text=True, check=False,
            encoding="utf-8", errors="replace", timeout=300,
        )
        check("17a dry-run parity: exits 0", proc.returncode == 0,
              f"rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}")
        check("17b dry-run parity: the --project smuggle is annotated SKIP",
              "SKIP (command overrides --project)" in proc.stdout,
              f"out={proc.stdout!r}")
        check("17c dry-run parity: the conflicting command is annotated SKIP",
              "SKIP (conflicting command for '灵根' (already queued))"
              in proc.stdout, f"out={proc.stdout!r}")
        check("17d dry-run parity: summary counts both skips",
              "3 command(s), 2 would be skipped (invalid or conflicting)"
              in proc.stdout, f"out={proc.stdout!r}")
        check("17e dry-run parity: nothing executed (glossary unchanged)",
              (root / "glossary.json").read_bytes() == before, "")


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_cjk_suggestion()
    case_2_valid_suggestions()
    case_3_empty_and_missing()
    case_4_out_of_scope()
    case_5_run_commands_integration()
    case_6_mundane_report_parsing()
    case_7_mundane_retire_run()
    case_8_new_format_report_parsing()
    case_9_noop_classification()
    case_10_project_override_guard()
    case_11_argparse_abbreviation()
    case_12_conflict_guard()
    case_13_report_staleness()
    case_14_subprocess_timeout()
    case_15_staleness_cli_refusal()
    case_16_category_guard()
    case_17_dryrun_skip_parity()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
