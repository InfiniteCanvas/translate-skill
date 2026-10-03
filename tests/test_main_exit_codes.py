"""Tests for main()'s [FAIL] exit-code mapping on the glossary-count path (H7).

`glossary count` reads every source chapter (read_chapter -> yaml), and a
scraped batch routinely contains a bad file (broken YAML frontmatter, or
non-UTF-8 bytes). The chapter read raises ValueError, cmd_review's
dispatcher maps it to a clean "[FAIL] cannot count - ..." line on stderr
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

import subprocess
import sys
import tempfile
from pathlib import Path

# scripts/ lives at novel-translator/scripts relative to this file
# (CWD-independent); the child (translate.py) extends sys.path itself.
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
TRANSLATE = SCRIPTS / "translate.py"

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


# ---------------------------------------------------------------------- cases


def case_1_bad_frontmatter_is_fail_exit_2() -> None:
    """A chapter with unparseable YAML frontmatter -> exit 2, one [FAIL]
    line on stderr naming the count guard, no traceback anywhere."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        source = root / "source"
        source.mkdir(parents=True)
        (source / "Chapter_001.md").write_text(
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
              "cannot count" in combined and "Chapter_001.md" in combined,
              f"combined={combined!r}")


def case_2_healthy_project_exits_0() -> None:
    """Control: the same command on a readable chapter with the term at
    the default significance threshold exits 0 -- the exit-2 in case 1
    comes from the corrupt chapter, not the invocation itself."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        source = root / "source"
        source.mkdir(parents=True)
        (source / "Chapter_001.md").write_text(
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
    # `glossary search` with fuzzy_max_distance: null
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

    # `glossary count` with min_term_occurrences: null
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "config.json").write_text(
            '{"providers": {}, "min_term_occurrences": null}\n',
            encoding="utf-8", newline="\n",
        )
        source = root / "source"
        source.mkdir(parents=True)
        (source / "Chapter_001.md").write_text(
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

    # Controls: the keys absent -> both commands exit normally.
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
        (source / "Chapter_001.md").write_text(
            "灵根初现。他又感到体内的灵根跳动，灵根温暖。\n",
            encoding="utf-8", newline="\n",
        )
        proc = run_cli(["glossary", "count", TERM, "--project", str(root)])
        check("3j control count: key absent exits 0 at the threshold",
              proc.returncode == 0 and "[FAIL]" not in (proc.stderr or ""),
              f"rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}")


def main() -> int:
    case_1_bad_frontmatter_is_fail_exit_2()
    case_2_healthy_project_exits_0()
    case_3_null_numeric_config_is_fail_exit_2()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
