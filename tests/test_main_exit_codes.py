"""Tests for main()'s [FAIL] exit-code mapping: the glossary-count guard
(H7) plus the remaining dispatch try-block arms.

`glossary count` reads every source chapter (read_chapter -> yaml), and a
scraped batch routinely contains a bad file (broken YAML frontmatter, or
non-UTF-8 bytes). The chapter read raises ValueError, and the count guard
in _cmd_glossary_count (translate.py) re-raises it as a CliError that
main() maps to a clean "[FAIL] cannot count - ..." line on stderr
with exit code 2 -- never a traceback, whose exit code 1 would misread as
"below threshold" for batch callers.

Case 1 drives the CLI as a real subprocess (sys.executable, exactly like
fix.run_commands spawns report commands) against a project whose only
chapter has unparseable frontmatter; case 2 is the healthy control (the
term occurs three times, threshold met) so the exit-2 assertion cannot
pass vacuously through some unrelated setup failure.

Case 3 pins the numeric-config coercion: a key PRESENT with a JSON null
(`.get(key, default)` returns None, so the default never rescues) must
map to a clean [FAIL] exit 2 through config.get_number's ValueError --
never a raw int(None) TypeError traceback -- for both consuming commands
(`glossary search` reads fuzzy_max_distance, `glossary count` reads
min_term_occurrences), with key-absent controls exiting normally.

Cases 4-6 pin the remaining arms of main()'s dispatch try-block, which the
subprocess cases above cannot reach without contrived fixtures: a
PipelineError escaping the dispatch -> exit 2, a raw OSError -> exit 2,
and KeyboardInterrupt -> exit 130. These run IN PROCESS (translate imported
directly; scripts/ put on sys.path like the file's fixture paths):
translate.cmd_status -- the dispatch target a lightweight `status` call
resolves to -- is swapped for a raiser with orig/restore in try/finally
(the attribute-swap convention, no unittest.mock), and main() runs under
redirect_stdout/redirect_stderr. In-process monkeypatching is the robust
pattern for the Ctrl-C arm especially: no signal games on Windows. main()
RETURNS its code (sys.exit fires only under __main__), so the return value
is the contract; each arm must print its documented [FAIL] line on stderr
and never a traceback.

Self-contained PASS/FAIL script (no pytest). The subprocess imports the
same interpreter that runs this script, so the lib deps (pyyaml,
requests, ebooklib, pillow) must be importable -- run via uv (deps
declared inline below):

    uv run tests/test_main_exit_codes.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0"]
# ///
from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
TRANSLATE = SCRIPTS / "translate.py"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import translate
from lib import client, pipeline

PASSED = 0
FAILED: list[str] = []

TERM = "灵根"


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"PASS  {name}")
    else:
        FAILED.append(name)
        print(f"FAIL  {name}" + (f"  [{detail}]" if detail else ""))


def run_count(project: Path) -> subprocess.CompletedProcess:
    """`glossary count <TERM> --project <dir>` as a child process; UTF-8
    decode matches the child's own stream reconfiguration (Windows locale
    default would otherwise raise on the CJK term)."""
    return subprocess.run(
        [sys.executable, str(TRANSLATE), "glossary", "count", TERM,
         "--project", str(project)],
        capture_output=True, text=True, check=False,
        encoding="utf-8", errors="replace", timeout=300,
    )


def run_cli(argv: list[str]) -> subprocess.CompletedProcess:
    """Arbitrary CLI invocation as a child process (run_count's decode
    conventions)."""
    return subprocess.run(
        [sys.executable, str(TRANSLATE), *argv],
        capture_output=True, text=True, check=False,
        encoding="utf-8", errors="replace", timeout=300,
    )


def run_main_inproc(argv: list[str]) -> tuple[str, str, int]:
    """translate.main() in-process with both streams captured: the arms
    print through sys.stdout/sys.stderr, which redirect_* swap, and main()
    RETURNS the exit code (sys.exit fires only under __main__)."""
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        code = translate.main(argv)
    return out_buf.getvalue(), err_buf.getvalue(), code


def case_1_bad_frontmatter_is_fail_exit_2() -> None:
    """A chapter with unparseable YAML frontmatter -> exit 2, one [FAIL]
    line on stderr naming the count guard, no traceback anywhere."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        source = root / "source"
        source.mkdir(parents=True)
        (source / "CHAPTER_0001.md").write_text(
            "---\n"
            "chapter_title: [unclosed\n"
            "---\n"
            "\n"
            "灵根 appears here.\n",
            encoding="utf-8", newline="\n",
        )

        proc = run_count(root)
        combined = (proc.stdout or "") + (proc.stderr or "")

        check("1a bad frontmatter: exit code 2", proc.returncode == 2,
              f"rc={proc.returncode} stderr={proc.stderr!r}")
        check("1b bad frontmatter: [FAIL] on stderr", "[FAIL]" in (proc.stderr or ""),
              f"stderr={proc.stderr!r}")
        check("1c bad frontmatter: no traceback", "Traceback" not in combined)
        check("1d bad frontmatter: the count guard names the failure",
              "cannot count" in combined and "CHAPTER_0001.md" in combined,
              f"combined={combined!r}")


def case_2_healthy_project_exits_0() -> None:
    """Control: the same command on a readable chapter with the term at
    the default significance threshold exits 0 -- the exit-2 in case 1
    comes from the corrupt chapter, not the invocation itself."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        source = root / "source"
        source.mkdir(parents=True)
        (source / "CHAPTER_0001.md").write_text(
            "第一章\n\n灵根初现。他又感到体内的灵根跳动，灵根温暖。\n",
            encoding="utf-8", newline="\n",
        )

        proc = run_count(root)
        combined = (proc.stdout or "") + (proc.stderr or "")

        check("2a healthy control: exit code 0", proc.returncode == 0,
              f"rc={proc.returncode} combined={combined!r}")
        check("2b healthy control: threshold met, no [FAIL]",
              "[ok]" in combined and "[FAIL]" not in combined,
              f"combined={combined!r}")


def case_3_null_numeric_config_is_fail_exit_2() -> None:
    """A numeric config key PRESENT with a JSON null maps to a clean [FAIL]
    exit 2 (config.get_number's ValueError through main()), never a raw
    int(None) TypeError traceback: `glossary search` reads
    fuzzy_max_distance, `glossary count` reads min_term_occurrences. The
    key-absent controls exit normally, so the exit-2 assertions cannot
    pass through an unrelated setup failure."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "config.json").write_text(
            '{"providers": {}, "fuzzy_max_distance": null}\n',
            encoding="utf-8", newline="\n",
        )
        proc = run_cli(["glossary", "search", TERM, "--project", str(root)])
        combined = (proc.stdout or "") + (proc.stderr or "")
        check("3a search null key: exit code 2", proc.returncode == 2,
              f"rc={proc.returncode} stderr={proc.stderr!r}")
        check("3b search null key: [FAIL] on stderr",
              "[FAIL]" in (proc.stderr or ""), f"stderr={proc.stderr!r}")
        check("3c search null key: no traceback", "Traceback" not in combined)
        check("3d search null key: the error names the config key",
              "fuzzy_max_distance" in combined, f"combined={combined!r}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "config.json").write_text(
            '{"providers": {}, "min_term_occurrences": null}\n',
            encoding="utf-8", newline="\n",
        )
        source = root / "source"
        source.mkdir(parents=True)
        (source / "CHAPTER_0001.md").write_text(
            "灵根初现。他又感到体内的灵根跳动，灵根温暖。\n",
            encoding="utf-8", newline="\n",
        )
        proc = run_cli(["glossary", "count", TERM, "--project", str(root)])
        combined = (proc.stdout or "") + (proc.stderr or "")
        check("3e count null key: exit code 2", proc.returncode == 2,
              f"rc={proc.returncode} stderr={proc.stderr!r}")
        check("3f count null key: [FAIL] on stderr",
              "[FAIL]" in (proc.stderr or ""), f"stderr={proc.stderr!r}")
        check("3g count null key: no traceback", "Traceback" not in combined)
        check("3h count null key: the error names the config key",
              "min_term_occurrences" in combined, f"combined={combined!r}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "config.json").write_text(
            '{"providers": {}}\n', encoding="utf-8", newline="\n")
        (root / "glossary.json").write_text(
            '{"terms": [{"source": "灵根", "variants": [], '
            '"translation": "spirit root"}]}\n',
            encoding="utf-8", newline="\n",
        )
        proc = run_cli(["glossary", "search", TERM, "--project", str(root)])
        check("3i control search: key absent exits 0 with a match",
              proc.returncode == 0 and "[FAIL]" not in (proc.stderr or ""),
              f"rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}")
        source = root / "source"
        source.mkdir(parents=True)
        (source / "CHAPTER_0001.md").write_text(
            "灵根初现。他又感到体内的灵根跳动，灵根温暖。\n",
            encoding="utf-8", newline="\n",
        )
        proc = run_cli(["glossary", "count", TERM, "--project", str(root)])
        check("3j control count: key absent exits 0 at the threshold",
              proc.returncode == 0 and "[FAIL]" not in (proc.stderr or ""),
              f"rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}")


def _dispatch_raiser(exc: BaseException):
    """A cmd_status stand-in whose body raises `exc` -- the `status`
    subcommand is the cheapest dispatch target, and the patched function is
    the first thing main()'s try block calls, so nothing on disk matters."""
    def func(args, project_dir):
        raise exc
    return func


def case_4_pipeline_error_arm() -> None:
    """A PipelineError escaping the dispatch -> [FAIL] line naming the
    error on stderr, exit 2, no traceback (main()'s second except arm)."""
    orig = translate.cmd_status
    translate.cmd_status = _dispatch_raiser(pipeline.PipelineError("boom"))
    try:
        with tempfile.TemporaryDirectory() as td:
            out, err, code = run_main_inproc(["status", "--project", td])
    finally:
        translate.cmd_status = orig
    check("4a PipelineError arm: exit code 2", code == 2, f"rc={code}")
    check("4b PipelineError arm: [FAIL] names the error on stderr",
          "[FAIL]" in err and "boom" in err, f"err={err!r}")
    check("4c PipelineError arm: no traceback", "Traceback" not in out + err,
          f"out={out!r} err={err!r}")


def case_5_os_error_arm() -> None:
    """A raw OSError escaping the dispatch -> [FAIL] '<Type>: msg' on
    stderr, exit 2, no traceback (main()'s last except arm)."""
    orig = translate.cmd_status
    translate.cmd_status = _dispatch_raiser(OSError("boom"))
    try:
        with tempfile.TemporaryDirectory() as td:
            out, err, code = run_main_inproc(["status", "--project", td])
    finally:
        translate.cmd_status = orig
    check("5a OSError arm: exit code 2", code == 2, f"rc={code}")
    check("5b OSError arm: [FAIL] names the type and message on stderr",
          "[FAIL]" in err and "OSError: boom" in err, f"err={err!r}")
    check("5c OSError arm: no traceback", "Traceback" not in out + err,
          f"out={out!r} err={err!r}")


def case_6_keyboard_interrupt_arm() -> None:
    """A KeyboardInterrupt escaping the dispatch -> the interrupted [FAIL]
    line on stderr and exit 130 (main()'s Ctrl-C arm; file-formats.md
    documents the code)."""
    orig = translate.cmd_status
    translate.cmd_status = _dispatch_raiser(KeyboardInterrupt())
    try:
        with tempfile.TemporaryDirectory() as td:
            out, err, code = run_main_inproc(["status", "--project", td])
    finally:
        translate.cmd_status = orig
    check("6a KeyboardInterrupt arm: exit code 130", code == 130,
          f"rc={code}")
    check("6b KeyboardInterrupt arm: [FAIL] interrupted line on stderr",
          "[FAIL]" in err and "interrupted" in err, f"err={err!r}")
    check("6c KeyboardInterrupt arm: no traceback",
          "Traceback" not in out + err, f"out={out!r} err={err!r}")


def case_7_provider_failure_arms() -> None:
    """Both provider arms -> exit 3, one [FAIL] line, no traceback.

    Exit 3 exists because 1 and 2 are already spoken for by documented
    meanings: 1 is "a chapter ended needs-review / a run degraded", 2 is "usage
    or setup error". A provider that cannot be reached or refuses the request is
    neither, and an operator or wrapper script needs to tell "your key is dead"
    apart from "this chapter needs a human".

    Two arms, because the distinction survives in the message even though the
    code does not:
      * LLMFatal  -- the provider said retrying cannot help.
      * LLMError  -- every retry was spent.
    """
    for name, exc, expect in [
        ("7a LLMFatal", client.LLMFatal("HTTP 400 from x: provider code 1210 "
                                        "(irrecoverable - retrying cannot help)"),
         "retrying cannot help"),
        ("7b LLMError", client.LLMError("HTTP 503 from x after 4 attempts: nope"),
         "after 4 attempts"),
    ]:
        orig = translate.cmd_status
        translate.cmd_status = _dispatch_raiser(exc)
        try:
            with tempfile.TemporaryDirectory() as td:
                out, err, code = run_main_inproc(["status", "--project", td])
        finally:
            translate.cmd_status = orig
        check(f"{name}: exit code 3", code == 3, f"rc={code}")
        check(f"{name}: [FAIL] on stderr naming the cause",
              "[FAIL]" in err and expect in err, f"err={err!r}")
        check(f"{name}: no traceback", "Traceback" not in out + err,
              f"out={out!r} err={err!r}")


def case_8_llmfatal_is_never_absorbed_by_a_stage_guard() -> None:
    """The mechanism, end to end through the real pipeline.

    An LLMFatal raised inside a chapter must leave `run_range` rather than
    marking the chapter needs-review and moving on -- which is what turned a
    dead API key into a whole batch of needs-review chapters and a misleading
    exit 1. The stage guards do that for free because LLMFatal is also a
    PipelineError; this asserts the batch loop's own guard is what does it."""
    def _boom(project_dir, file, cfg, force=False):
        raise client.LLMFatal("HTTP 429 from x: provider code 1113")

    orig = pipeline.run_chapter
    pipeline.run_chapter = _boom
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "source").mkdir(parents=True)
            (root / "source" / "CHAPTER_0001.md").write_text(
                "---\nchapter_title: One\n---\n\n文字。\n",
                encoding="utf-8", newline="\n")
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                raised = False
                try:
                    pipeline.run_range(root, ["CHAPTER_0001.md"], {}, force=False)
                except client.LLMFatal:
                    raised = True
                except Exception:
                    raised = False
    finally:
        pipeline.run_chapter = orig
    check("8a run_range lets an LLMFatal out instead of swallowing it",
          raised, "the chapter was absorbed into needs-review")


def main() -> int:
    case_1_bad_frontmatter_is_fail_exit_2()
    case_2_healthy_project_exits_0()
    case_3_null_numeric_config_is_fail_exit_2()
    case_4_pipeline_error_arm()
    case_5_os_error_arm()
    case_6_keyboard_interrupt_arm()
    case_7_provider_failure_arms()
    case_8_llmfatal_is_never_absorbed_by_a_stage_guard()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
